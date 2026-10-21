from __future__ import annotations

import asyncio
import os
import shutil
import signal
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repopilot.capabilities import WorkspaceCapabilityPolicy, default_workspace_policy
from repopilot.config import Settings
from repopilot.execution import CleanupFailed, controlled, current_control, join_task
from repopilot.models import FileEdit
from repopilot.retrieval import HybridCodeRetriever
from repopilot.security import (
    CapabilityAction,
    CapabilityError,
    CapabilityReason,
    NormalizedWorkspacePath,
    SecurityError,
    WorkspacePathError,
    normalize_workspace_path,
    safe_workspace_path,
    validate_branch,
    validate_repository_url,
)

_TEXT_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".py",
    ".rs",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
_IGNORED_PARTS = {".git", ".venv", "node_modules", "dist", "build", "vendor"}


@dataclass(slots=True)
class RepositoryContext:
    tree: list[str]
    files: dict[str, str]
    editable_paths: tuple[str, ...] | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    query_terms: list[str] = field(default_factory=list)
    strategy: str = "legacy"
    candidate_count: int = 0
    selected_chars: int = 0
    skipped_for_budget: int = 0
    max_chars: int = 70_000
    capability_policy_version: str = "legacy"

    def render(self, max_chars: int | None = None) -> str:
        limit = self.max_chars if max_chars is None else min(max_chars, self.max_chars)
        if limit <= 0:
            return ""
        evidence_lines = [
            (
                f"- {item.get('path')} score={item.get('score')} "
                f"terms={item.get('matched_terms', [])} "
                f"symbols={item.get('matched_symbols', [])} "
                f"related={item.get('related_paths', [])}"
            )
            for item in self.evidence
        ]
        prefix = "\n".join(
            [
                f"Retrieval strategy: {self.strategy}",
                f"Query terms: {', '.join(self.query_terms)}",
                "Retrieval evidence:",
                *evidence_lines,
                "Repository tree:",
                *self.tree[:200],
            ]
        )
        file_sections: list[str] = []
        for path, content in self.files.items():
            file_sections.append(f"\n--- {path} ---\n{content}")

        file_budget = max(0, limit - min(12_000, limit // 4))
        rendered_files: list[str] = []
        rendered_file_chars = 0
        for section in file_sections:
            if rendered_file_chars + len(section) > file_budget:
                continue
            rendered_files.append(section)
            rendered_file_chars += len(section)
        prefix_budget = max(0, limit - rendered_file_chars)
        return f"{prefix[:prefix_budget]}{''.join(rendered_files)}"


async def run_process_bytes(
    *args: str,
    cwd: Path | None = None,
    timeout_seconds: int = 120,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    control = current_control()
    if control:
        timeout_seconds = control.timeout(timeout_seconds)
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
            start_new_session=True,
        )
    )
    process = None
    resource = None
    try:
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await join_task(spawn)
            raise
        resource = {"kind": "process_group", "id": str(process.pid)}
        if control:
            await control.track(resource)
            control.check()
        stdout, stderr = await controlled(process.communicate(input_bytes), timeout_seconds)
        return process.returncode, stdout, stderr
    finally:
        if process:
            try:
                # Descendants share this newly created session, never another task's group.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(process.wait(), timeout=10)
            except (OSError, TimeoutError) as error:
                raise CleanupFailed(
                    {
                        **(resource or {"kind": "process_group", "id": str(process.pid)}),
                        "action": "kill_and_wait",
                        "error": type(error).__name__,
                    }
                ) from None
            if control and resource:
                await control.track(resource, remove=True)


async def run_process(
    *args: str,
    cwd: Path | None = None,
    timeout_seconds: int = 120,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    code, stdout, stderr = await run_process_bytes(
        *args, cwd=cwd, timeout_seconds=timeout_seconds, env=env
    )
    return code, stdout.decode(errors="replace"), stderr.decode(errors="replace")


class WorkspaceManager:
    def __init__(
        self,
        settings: Settings,
        capability_policy: WorkspaceCapabilityPolicy | None = None,
    ):
        self.settings = settings
        self.capability_policy = capability_policy or default_workspace_policy(
            max_file_bytes=settings.max_file_bytes
        )

    async def base_commit(self, workspace: Path) -> str:
        code, stdout, _ = await run_process("git", "rev-parse", "--verify", "HEAD", cwd=workspace)
        if code != 0:
            raise SecurityError("Could not resolve the prepared repository base commit")
        return stdout.strip()

    def base_repository(self, workspace: Path) -> Path:
        """Trusted commit source; exported evaluation workspaces override this."""
        return workspace

    async def prepare(
        self,
        task_id: str,
        repository_url: str,
        base_branch: str,
    ) -> Path:
        validate_repository_url(repository_url)
        validate_branch(base_branch)
        workspace = safe_workspace_path(self.settings.workspace_root, task_id)
        if (workspace / ".git").is_dir():
            return workspace
        workspace.mkdir(parents=True, exist_ok=False)
        if repository_url.startswith("demo://"):
            await self._prepare_demo(workspace)
            return workspace

        code, _, stderr = await run_process(
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            base_branch,
            "--",
            repository_url,
            str(workspace),
            timeout_seconds=180,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if code != 0:
            raise RuntimeError(f"Repository clone failed: {stderr[-1_500:]}")
        return workspace

    async def _prepare_demo(self, workspace: Path) -> None:
        source = self.settings.demo_repository_root.resolve()
        if not source.exists():
            raise FileNotFoundError(f"Demo repository is missing: {source}")
        shutil.copytree(source, workspace, dirs_exist_ok=True)
        commands = [
            ("git", "init", "-b", "main"),
            ("git", "config", "user.name", "RepoPilot Demo"),
            ("git", "config", "user.email", "repopilot@example.invalid"),
            ("git", "add", "."),
            ("git", "commit", "-m", "chore: seed demo repository"),
        ]
        for command in commands:
            code, _, stderr = await run_process(*command, cwd=workspace)
            if code != 0:
                raise RuntimeError(f"Could not initialize demo repository: {stderr}")

    def inspect(
        self,
        workspace: Path,
        issue_text: str,
        search_terms: list[str],
    ) -> RepositoryContext:
        all_files: list[tuple[str, Path]] = []
        for current, directories, filenames in os.walk(workspace, followlinks=False):
            current_path = Path(current)
            directories[:] = sorted(
                directory
                for directory in directories
                if directory not in _IGNORED_PARTS and not (current_path / directory).is_symlink()
            )
            for filename in sorted(filenames):
                path = current_path / filename
                try:
                    file_stat = path.lstat()
                    relative = path.relative_to(workspace).as_posix()
                    normalized = normalize_workspace_path(relative)
                    absolute = safe_workspace_path(workspace, normalized.value)
                except (OSError, SecurityError, ValueError):
                    continue
                if (
                    not stat.S_ISREG(file_stat.st_mode)
                    or file_stat.st_nlink > 1
                    or path.suffix.lower() not in _TEXT_SUFFIXES
                    or file_stat.st_size > self.settings.max_file_bytes
                    or not self.capability_policy.permits(CapabilityAction.READ, normalized.value)
                ):
                    continue
                all_files.append((normalized.value, absolute))

        contents: dict[str, str] = {}
        for relative, absolute in sorted(all_files):
            try:
                # Intent hashes and descriptor writes compare exact UTF-8 bytes;
                # universal newline translation would make CRLF context stale.
                with absolute.open(encoding="utf-8", newline="") as source:
                    content = source.read()
            except (OSError, UnicodeDecodeError):
                continue
            contents[relative] = content

        result = HybridCodeRetriever(
            max_files=self.settings.max_context_files,
            max_context_chars=self.settings.max_context_chars,
        ).retrieve(contents, issue_text=issue_text, search_terms=search_terms)
        return RepositoryContext(
            tree=sorted(contents),
            files={hit.path: hit.content for hit in result.hits},
            editable_paths=tuple(
                hit.path
                for hit in result.hits
                if self.capability_policy.permits(CapabilityAction.WRITE, hit.path)
            ),
            evidence=[hit.evidence() for hit in result.hits],
            query_terms=list(result.query_terms),
            strategy=result.strategy,
            candidate_count=result.candidate_count,
            selected_chars=result.selected_chars,
            skipped_for_budget=result.skipped_for_budget,
            max_chars=self.settings.max_context_chars,
            capability_policy_version=self.capability_policy.version,
        )

    def _capability_path(
        self,
        workspace: Path,
        raw_path: str,
        action: CapabilityAction,
    ) -> tuple[NormalizedWorkspacePath, Path]:
        try:
            normalized = normalize_workspace_path(raw_path)
            target = safe_workspace_path(workspace, normalized.value)
        except (OSError, SecurityError) as error:
            reason = (
                error.reason
                if isinstance(error, WorkspacePathError)
                else CapabilityReason.INVALID_PATH
            )
            raise CapabilityError(
                reason,
                action,
                raw_path,
                str(error),
                policy_version=self.capability_policy.version,
            ) from error
        return normalized, target

    def _existing_file_stat(
        self,
        target: Path,
        normalized: NormalizedWorkspacePath,
        action: CapabilityAction,
    ) -> os.stat_result | None:
        try:
            target_stat = target.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(target_stat.st_mode):
            raise CapabilityError(
                CapabilityReason.UNSAFE_LINK,
                action,
                normalized.value,
                "symbolic link targets are not accepted",
                policy_version=self.capability_policy.version,
            )
        if not stat.S_ISREG(target_stat.st_mode):
            raise CapabilityError(
                CapabilityReason.SPECIAL_FILE,
                action,
                normalized.value,
                "target is not a regular file",
                policy_version=self.capability_policy.version,
            )
        if target_stat.st_nlink > 1:
            raise CapabilityError(
                CapabilityReason.HARDLINK,
                action,
                normalized.value,
                "hard-linked files are not accepted at the write boundary",
                policy_version=self.capability_policy.version,
            )
        return target_stat

    def apply_edits(
        self,
        workspace: Path,
        edits: list[FileEdit],
        *,
        expected_contents: dict[str, str] | None = None,
        expected_absent: Iterable[str] | None = None,
    ) -> list[str]:
        # Validate the complete proposal before making any edit. The graph has one
        # writer; this additionally catches stale model context at the write boundary.
        # It is not a transaction protocol for arbitrary concurrent filesystem writers.
        expected_by_identity: dict[str, tuple[str, str]] = {}
        for raw_path, content in (expected_contents or {}).items():
            try:
                normalized = normalize_workspace_path(raw_path)
            except SecurityError as error:
                raise CapabilityError(
                    CapabilityReason.INVALID_PATH,
                    CapabilityAction.WRITE,
                    raw_path,
                    str(error),
                    policy_version=self.capability_policy.version,
                ) from error
            if normalized.identity in expected_by_identity:
                raise CapabilityError(
                    CapabilityReason.DUPLICATE_PATH,
                    CapabilityAction.WRITE,
                    normalized.value,
                    "supplied context contains aliased duplicate paths",
                    policy_version=self.capability_policy.version,
                )
            expected_by_identity[normalized.identity] = (normalized.value, content)

        absent_by_identity: dict[str, str] = {}
        for raw_path in expected_absent or ():
            try:
                normalized = normalize_workspace_path(raw_path)
            except SecurityError as error:
                raise CapabilityError(
                    CapabilityReason.INVALID_PATH,
                    CapabilityAction.CREATE,
                    raw_path,
                    str(error),
                    policy_version=self.capability_policy.version,
                ) from error
            if normalized.identity in absent_by_identity:
                raise CapabilityError(
                    CapabilityReason.DUPLICATE_PATH,
                    CapabilityAction.CREATE,
                    normalized.value,
                    "expected-absent paths contain aliases",
                    policy_version=self.capability_policy.version,
                )
            absent_by_identity[normalized.identity] = normalized.value

        prepared: list[tuple[FileEdit, str, Path, str | None, bool]] = []
        seen: dict[str, str] = {}
        for edit in edits:
            try:
                normalized = normalize_workspace_path(edit.path)
            except SecurityError as error:
                raise CapabilityError(
                    CapabilityReason.INVALID_PATH,
                    CapabilityAction.WRITE,
                    edit.path,
                    str(error),
                    policy_version=self.capability_policy.version,
                ) from error
            duplicate = seen.get(normalized.identity)
            if duplicate is not None:
                raise CapabilityError(
                    CapabilityReason.DUPLICATE_PATH,
                    CapabilityAction.WRITE,
                    normalized.value,
                    f"generated path aliases duplicate {duplicate}",
                    policy_version=self.capability_policy.version,
                )
            seen[normalized.identity] = normalized.value
            normalized, target = self._capability_path(
                workspace, normalized.value, CapabilityAction.WRITE
            )
            size = len(edit.content.encode("utf-8"))
            if size > self.settings.max_file_bytes:
                raise CapabilityError(
                    CapabilityReason.FILE_TOO_LARGE,
                    CapabilityAction.WRITE,
                    normalized.value,
                    "generated content exceeds the workspace byte limit",
                    policy_version=self.capability_policy.version,
                )
            target_stat = self._existing_file_stat(target, normalized, CapabilityAction.WRITE)
            if target_stat is None:
                expected_path = absent_by_identity.get(normalized.identity)
                if expected_path is not None and expected_path != normalized.value:
                    raise CapabilityError(
                        CapabilityReason.PATH_ALIAS,
                        CapabilityAction.CREATE,
                        normalized.value,
                        f"path casing does not match expected target {expected_path}",
                        policy_version=self.capability_policy.version,
                    )
                self.capability_policy.require(
                    CapabilityAction.CREATE,
                    normalized,
                    size=size,
                    expected_absent=expected_path is not None,
                )
                prepared.append((edit, normalized.value, target, None, True))
                continue

            self.capability_policy.require(CapabilityAction.WRITE, normalized, size=size)
            if normalized.identity in absent_by_identity:
                raise CapabilityError(
                    CapabilityReason.TARGET_EXISTS,
                    CapabilityAction.CREATE,
                    normalized.value,
                    "expected-absent target already exists",
                    policy_version=self.capability_policy.version,
                )
            with target.open(encoding="utf-8", newline="") as source:
                previous = source.read()
            if expected_contents is not None:
                expected = expected_by_identity.get(normalized.identity)
                if expected is None:
                    raise CapabilityError(
                        CapabilityReason.OUTSIDE_CONTEXT,
                        CapabilityAction.WRITE,
                        normalized.value,
                        "generated edit is outside the supplied readable context",
                        policy_version=self.capability_policy.version,
                    )
                expected_path, expected_content = expected
                if expected_path != normalized.value:
                    raise CapabilityError(
                        CapabilityReason.PATH_ALIAS,
                        CapabilityAction.WRITE,
                        normalized.value,
                        f"path casing does not match supplied context {expected_path}",
                        policy_version=self.capability_policy.version,
                    )
                if previous != expected_content:
                    raise CapabilityError(
                        CapabilityReason.STALE_CONTENT,
                        CapabilityAction.WRITE,
                        normalized.value,
                        "workspace changed since model context was read",
                        policy_version=self.capability_policy.version,
                    )
            prepared.append((edit, normalized.value, target, previous, False))
        changed: list[str] = []
        for edit, normalized_path, _target, previous, is_create in prepared:
            if previous != edit.content:
                self._write_prepared_file(
                    workspace, normalized_path, edit.content, previous, is_create
                )
                changed.append(normalized_path)
        return changed

    def _write_prepared_file(
        self, workspace: Path, path: str, content: str, previous: str | None, is_create: bool
    ) -> None:
        """Descriptor-relative staging writer; never follows a replaced path link.

        The generation coordinator supplies a private tree and serializes internal
        writers. This method also remains usable by trusted one-shot evaluators.
        It does not promise a transaction for arbitrary external filesystem writers.
        """
        normalized = normalize_workspace_path(path)
        action = CapabilityAction.CREATE if is_create else CapabilityAction.WRITE
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_fd = os.open(workspace, flags)
        try:
            parts = normalized.value.split("/")
            for part in parts[:-1]:
                try:
                    child_fd = os.open(part, flags, dir_fd=directory_fd)
                except FileNotFoundError:
                    if not is_create:
                        raise
                    os.mkdir(part, mode=0o755, dir_fd=directory_fd)
                    child_fd = os.open(part, flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = child_fd
            file_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            if is_create:
                file_flags |= os.O_CREAT | os.O_EXCL
            try:
                fd = os.open(parts[-1], file_flags, 0o644, dir_fd=directory_fd)
            except FileExistsError as error:
                raise CapabilityError(
                    CapabilityReason.TARGET_EXISTS,
                    action,
                    path,
                    "target appeared after the expected-absent preflight",
                    policy_version=self.capability_policy.version,
                ) from error
            with os.fdopen(fd, "r+b") as file:
                opened = os.fstat(file.fileno())
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                    raise SecurityError("Staging target changed type or acquired a hard link")
                if not is_create:
                    data = file.read(self.settings.max_file_bytes + 1)
                    if data != (previous or "").encode("utf-8"):
                        raise CapabilityError(
                            CapabilityReason.STALE_CONTENT,
                            action,
                            path,
                            "workspace changed after modification preflight",
                            policy_version=self.capability_policy.version,
                        )
                file.seek(0)
                file.write(content.encode("utf-8"))
                file.truncate()
                file.flush()
                os.fsync(file.fileno())
                current = os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino, current.st_nlink) != (
                    opened.st_dev,
                    opened.st_ino,
                    1,
                ):
                    raise SecurityError("Staging target was replaced during modification")
        finally:
            os.close(directory_fd)

    async def diff(self, workspace: Path) -> str:
        code, stdout, stderr = await run_process(
            "git", "diff", "--no-ext-diff", "--", ".", cwd=workspace
        )
        if code != 0:
            raise RuntimeError(f"Could not calculate git diff: {stderr}")
        return stdout[:100_000]
