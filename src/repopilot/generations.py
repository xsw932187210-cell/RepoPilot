"""Complete candidate generations, with a database CAS and retained staging journals.

An OS lock serializes supported writers sharing a workspace volume. It is not a
Worker ownership lease. Filesystem durability and the database remain two commits;
reconciliation reports incomplete/orphan artifacts and never silently adopts them.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import stat
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from repopilot.approvals import ApprovalConflict, CandidateRecord
from repopilot.candidates import (
    ArtifactStore,
    Candidate,
    candidate_identity,
    canonical_json,
    capture_manifest,
    digest_json,
    sync_directory,
)
from repopilot.db import Base, TaskRecord
from repopilot.models import FileEdit, TaskStatus
from repopilot.security import SecurityError

if TYPE_CHECKING:
    from repopilot.db import Database
    from repopilot.repository import WorkspaceManager


class GenerationConflict(ApprovalConflict):
    code = "stale_candidate_generation"

    def __init__(self) -> None:
        super().__init__(self.code)


class GenerationRecord(Base):
    __tablename__ = "candidate_generations"
    __table_args__ = (UniqueConstraint("task_id", "operation_id", name="uq_generation_operation"),)

    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"))
    parent_candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operation_id: Mapped[str] = mapped_column(String(64))


class GenerationHeadRecord(Base):
    __tablename__ = "generation_heads"

    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(Integer)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"))


async def require_current(session: AsyncSession, task_id: str, candidate_id: str) -> None:
    # All SQL writers lock the task first, including approval and task transitions.
    await session.execute(select(TaskRecord.id).where(TaskRecord.id == task_id).with_for_update())
    head = await session.get(GenerationHeadRecord, task_id)
    if head is None or head.candidate_id != candidate_id:
        raise GenerationConflict()


def _validate(parent: Candidate | None, candidate: Candidate) -> None:
    if candidate_identity(candidate) != candidate.candidate_id:
        raise SecurityError("Candidate identity mismatch")
    if parent is None and candidate.generation != 0:
        raise GenerationConflict()
    if parent is not None and (
        candidate.task_id != parent.task_id
        or candidate.generation != parent.generation + 1
        or candidate.base_sha != parent.base_sha
        or candidate.policy_version != parent.policy_version
    ):
        raise GenerationConflict()


class MemoryGenerationStore:
    """Offline graph adapter; deliberately makes no process-restart guarantee."""

    def __init__(self) -> None:
        self.heads: dict[str, Candidate] = {}
        self.operations: dict[tuple[str, str], Candidate] = {}

    async def current(self, task_id: str) -> Candidate | None:
        return self.heads.get(task_id)

    async def operation(self, task_id: str, operation_id: str) -> Candidate | None:
        return self.operations.get((task_id, operation_id))

    async def assert_current(self, candidate: Candidate) -> None:
        if self.heads.get(candidate.task_id) != candidate:
            raise GenerationConflict()

    async def commit(
        self, parent: Candidate | None, operation_id: str, candidate: Candidate
    ) -> Candidate:
        _validate(parent, candidate)
        existing = await self.operation(candidate.task_id, operation_id)
        if existing is not None:
            await self.assert_current(existing)
            if existing != candidate:
                raise GenerationConflict()
            return existing
        if self.heads.get(candidate.task_id) != parent:
            raise GenerationConflict()
        self.operations[(candidate.task_id, operation_id)] = candidate
        self.heads[candidate.task_id] = candidate
        return candidate


class SQLGenerationStore:
    def __init__(self, database: Database):
        self.database = database

    async def current(self, task_id: str) -> Candidate | None:
        async with self.database.sessions() as session:
            row = (
                await session.execute(
                    select(CandidateRecord)
                    .join(
                        GenerationHeadRecord,
                        GenerationHeadRecord.candidate_id == CandidateRecord.id,
                    )
                    .where(GenerationHeadRecord.task_id == task_id)
                )
            ).scalar_one_or_none()
            return Candidate.model_validate(row.payload) if row else None

    async def operation(self, task_id: str, operation_id: str) -> Candidate | None:
        async with self.database.sessions() as session:
            row = (
                await session.execute(
                    select(CandidateRecord)
                    .join(GenerationRecord, GenerationRecord.candidate_id == CandidateRecord.id)
                    .where(
                        GenerationRecord.task_id == task_id,
                        GenerationRecord.operation_id == operation_id,
                    )
                )
            ).scalar_one_or_none()
            return Candidate.model_validate(row.payload) if row else None

    async def assert_current(self, candidate: Candidate) -> None:
        async with self.database.sessions() as session, session.begin():
            await require_current(session, candidate.task_id, candidate.candidate_id)

    async def commit(
        self, parent: Candidate | None, operation_id: str, candidate: Candidate
    ) -> Candidate:
        _validate(parent, candidate)
        async with self.database.sessions() as session, session.begin():
            task = (
                await session.execute(
                    select(TaskRecord).where(TaskRecord.id == candidate.task_id).with_for_update()
                )
            ).scalar_one_or_none()
            if task is None or task.status != TaskStatus.RUNNING.value:
                raise GenerationConflict()
            head = await session.get(GenerationHeadRecord, candidate.task_id)
            existing = (
                await session.execute(
                    select(GenerationRecord).where(
                        GenerationRecord.task_id == candidate.task_id,
                        GenerationRecord.operation_id == operation_id,
                    )
                )
            ).scalar_one_or_none()
            if existing:
                if (
                    head is None
                    or head.candidate_id != existing.candidate_id
                    or existing.candidate_id != candidate.candidate_id
                ):
                    raise GenerationConflict()
                return candidate
            if parent is None:
                if head is not None or candidate.generation != 0:
                    raise GenerationConflict()
            elif head is None or (head.generation, head.candidate_id) != (
                parent.generation,
                parent.candidate_id,
            ):
                raise GenerationConflict()
            saved = await session.get(CandidateRecord, candidate.candidate_id)
            if saved is None:
                session.add(
                    CandidateRecord(
                        id=candidate.candidate_id,
                        task_id=candidate.task_id,
                        payload=candidate.model_dump(mode="json"),
                    )
                )
                await session.flush()
            elif saved.payload != candidate.model_dump(mode="json"):
                raise SecurityError("Candidate identity collision")
            session.add(
                GenerationRecord(
                    task_id=candidate.task_id,
                    generation=candidate.generation,
                    candidate_id=candidate.candidate_id,
                    parent_candidate_id=parent.candidate_id if parent else None,
                    operation_id=operation_id,
                )
            )
            await session.flush()
            if parent is None:
                session.add(
                    GenerationHeadRecord(
                        task_id=candidate.task_id,
                        generation=candidate.generation,
                        candidate_id=candidate.candidate_id,
                    )
                )
            else:
                changed = await session.execute(
                    update(GenerationHeadRecord)
                    .where(
                        GenerationHeadRecord.task_id == candidate.task_id,
                        GenerationHeadRecord.generation == parent.generation,
                        GenerationHeadRecord.candidate_id == parent.candidate_id,
                    )
                    .values(generation=candidate.generation, candidate_id=candidate.candidate_id)
                )
                if changed.rowcount != 1:
                    raise GenerationConflict()
        return candidate


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise SecurityError("Generation storage must not contain directory links")
    if not path.exists():
        _private_directory(path.parent)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass  # Another supported writer can have created the same task root.
        sync_directory(path.parent)
    if path.is_symlink() or not path.is_dir():
        raise SecurityError("Generation storage must not contain directory links")


def _journal(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f"journal-{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(canonical_json(payload))
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


class GenerationCoordinator:
    def __init__(
        self,
        artifacts: ArtifactStore,
        store: SQLGenerationStore | MemoryGenerationStore,
        workspaces: WorkspaceManager,
        *,
        fault: Callable[[str], None] | None = None,
    ):
        self.artifacts, self.store, self.workspaces = artifacts, store, workspaces
        self.root = artifacts.root.parent / ".staging"
        self.fault = fault or (lambda _: None)

    def _task_root(self, task_id: str) -> Path:
        _private_directory(self.root.parent)
        _private_directory(self.root)
        root = self.root / hashlib.sha256(task_id.encode()).hexdigest()
        _private_directory(root)
        return root

    @asynccontextmanager
    async def lock(self, task_id: str) -> AsyncIterator[Path]:
        root = self._task_root(task_id)
        fd = os.open(root / "writer.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
                raise SecurityError("Unsafe generation writer lock")
            # Never block the event loop while another supported writer is active.
            for _ in range(3000):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.01)
            else:
                raise GenerationConflict()
            yield root
        finally:
            os.close(fd)

    async def _recover(self, root: Path, task_id: str) -> list[dict[str, Any]]:
        reports = []
        for directory in sorted(await asyncio.to_thread(lambda: list(root.iterdir()))):
            if not directory.is_dir() and not directory.is_symlink():
                continue
            path = directory / "journal.json"
            if directory.is_symlink() or path.is_symlink():
                raise SecurityError("Unsafe staging journal")
            if not path.exists():
                reports.append({"staging_id": directory.name, "state": "incomplete"})
                continue
            try:
                payload = json.loads(path.read_bytes())
                if payload["task_id"] != task_id:
                    raise ValueError("wrong task")
                saved = await self.store.operation(task_id, payload["operation_id"])
                candidate = (
                    Candidate.model_validate(payload["candidate"])
                    if payload.get("candidate")
                    else None
                )
                if saved and candidate == saved:
                    self.artifacts.read_diff(saved)
                    with self.artifacts.materialize(saved):
                        pass
                    state = "committed"
                elif payload["phase"] in {"sealed", "committed"} and candidate:
                    self.artifacts.read_diff(candidate)
                    with self.artifacts.materialize(candidate):
                        pass
                    state = "orphan"
                else:
                    state = "incomplete"
            except (KeyError, ValueError, OSError, SecurityError):
                state = "corrupt"
            reports.append({"staging_id": path.parent.name, "state": state})
        return reports

    async def recover(self, task_id: str) -> list[dict[str, Any]]:
        async with self.lock(task_id) as root:
            reports = await self._recover(root, task_id)
            _journal(root / "recovery.json", {"task_id": task_id, "stages": reports})
            return reports

    def _new_stage(self, root: Path, task_id: str, operation: str, parent: Candidate | None):
        stage = root / uuid.uuid4().hex
        stage.mkdir(mode=0o700)
        sync_directory(root)
        payload = {
            "schema_version": "staging-v1",
            "task_id": task_id,
            "operation_id": operation,
            "parent_candidate_id": parent.candidate_id if parent else None,
            "phase": "writing",
            "candidate": None,
        }
        _journal(stage / "journal.json", payload)
        return stage, payload

    async def _seal_commit(
        self,
        stage: Path,
        payload: dict,
        tree: Path,
        task_id: str,
        base_sha: str,
        policy_version: str,
        parent: Candidate | None,
        base_repository: Path,
        expected_manifest: dict,
    ) -> Candidate:
        bundle_stage = stage / "artifact"
        bundle_stage.mkdir()
        candidate = await self.artifacts.freeze(
            tree,
            task_id=task_id,
            base_sha=base_sha,
            generation=parent.generation + 1 if parent else 0,
            policy_version=policy_version,
            base_repository=base_repository,
            staging=bundle_stage,
        )
        payload.update(phase="sealed", candidate=candidate.model_dump(mode="json"))
        _journal(stage / "journal.json", payload)
        sync_directory(bundle_stage)
        sync_directory(stage)
        self.fault("sealed")
        if candidate.manifest != expected_manifest or capture_manifest(tree, base_sha) != (
            expected_manifest
        ):
            raise SecurityError("Candidate tree changed while sealing the proposal")
        await self.store.commit(parent, payload["operation_id"], candidate)
        self.fault("committed")
        payload["phase"] = "committed"
        _journal(stage / "journal.json", payload)
        return candidate

    async def initialize(self, task_id: str, workspace: Path, base_sha: str) -> Candidate:
        operation = digest_json({"kind": "prepare-v1", "task_id": task_id, "base_sha": base_sha})
        async with self.lock(task_id) as root:
            await self._recover(root, task_id)
            existing = await self.store.operation(task_id, operation)
            if existing:
                await self.store.assert_current(existing)
                return existing
            if await self.store.current(task_id) is not None:
                raise GenerationConflict()
            stage, payload = self._new_stage(root, task_id, operation, None)
            before = capture_manifest(workspace, base_sha)
            candidate = await self._seal_commit(
                stage,
                payload,
                workspace,
                task_id,
                base_sha,
                self.workspaces.capability_policy.version,
                None,
                self.workspaces.base_repository(workspace),
                before,
            )
            return candidate

    async def apply(
        self,
        parent: Candidate,
        edits: list[FileEdit],
        *,
        expected_contents: dict[str, str],
        base_repository: Path,
    ) -> Candidate:
        operation = digest_json(
            {
                "kind": "apply-v1",
                "parent": parent.candidate_id,
                "edits": [edit.model_dump(mode="json") for edit in edits],
                "context": digest_json(expected_contents),
            }
        )
        async with self.lock(parent.task_id) as root:
            await self._recover(root, parent.task_id)
            existing = await self.store.operation(parent.task_id, operation)
            if existing:
                await self.store.assert_current(existing)
                return existing
            await self.store.assert_current(parent)
            stage, payload = self._new_stage(root, parent.task_id, operation, parent)
            tree = stage / "tree"
            tree.mkdir()
            self.artifacts.extract_to(parent, tree)
            self.fault("materialized")
            if capture_manifest(tree, parent.base_sha) != parent.manifest:
                raise SecurityError("Staging tree changed before modification")
            self.workspaces.apply_edits(tree, edits, expected_contents=expected_contents)
            # Every unedited byte, path and mode must remain identical to the parent.
            expected = {entry["path"]: dict(entry) for entry in parent.manifest["entries"]}
            for edit in edits:
                data = edit.content.encode("utf-8")
                if edit.path not in expected:
                    raise SecurityError("Generation edits must target existing context files")
                expected[edit.path].update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
            actual = capture_manifest(tree, parent.base_sha)
            if actual["entries"] != sorted(expected.values(), key=lambda entry: entry["path"]):
                raise SecurityError("Staging tree changed outside the complete proposal")
            self.fault("written")
            return await self._seal_commit(
                stage,
                payload,
                tree,
                parent.task_id,
                parent.base_sha,
                parent.policy_version,
                parent,
                base_repository,
                actual,
            )
