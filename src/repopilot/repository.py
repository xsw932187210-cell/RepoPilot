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


async def run_process(
    *args: str,
    cwd: Path | None = None,
    timeout_seconds: int = 120,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except (TimeoutError, asyncio.CancelledError) as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise TimeoutError(f"Command timed out: {args[0]}") from None
    return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


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
                if directory not in _IGNORED_PARTS
                and not (current_path / directory).is_symlink()
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
                content = absolute.read_text(encoding="utf-8")
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
            target_stat = self._existing_file_stat(
                target, normalized, CapabilityAction.WRITE
            )
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
            previous = target.read_text(encoding="utf-8")
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
        for edit, normalized_path, target, previous, is_create in prepared:
            target.parent.mkdir(parents=True, exist_ok=True)
            if previous != edit.content:
                if is_create:
                    try:
                        with target.open("x", encoding="utf-8") as file:
                            file.write(edit.content)
                    except FileExistsError as error:
                        raise CapabilityError(
                            CapabilityReason.TARGET_EXISTS,
                            CapabilityAction.CREATE,
                            normalized_path,
                            "target appeared after the expected-absent preflight",
                            policy_version=self.capability_policy.version,
                        ) from error
                else:
                    target.write_text(edit.content, encoding="utf-8")
                changed.append(normalized_path)
        return changed

    async def diff(self, workspace: Path) -> str:
        code, stdout, stderr = await run_process(
            "git", "diff", "--no-ext-diff", "--", ".", cwd=workspace
        )
        if code != 0:
            raise RuntimeError(f"Could not calculate git diff: {stderr}")
        return stdout[:100_000]
