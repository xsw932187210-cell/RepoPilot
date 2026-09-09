from __future__ import annotations

import asyncio
import os
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repopilot.config import Settings
from repopilot.models import FileEdit
from repopilot.retrieval import HybridCodeRetriever
from repopilot.security import (
    SecurityError,
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
    def __init__(self, settings: Settings):
        self.settings = settings

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
        all_files: list[Path] = []
        for path in workspace.rglob("*"):
            if not path.is_file() or any(part in _IGNORED_PARTS for part in path.parts):
                continue
            relative = path.relative_to(workspace)
            if (
                path.suffix.lower() in _TEXT_SUFFIXES
                and path.stat().st_size <= self.settings.max_file_bytes
            ):
                all_files.append(relative)

        contents: dict[str, str] = {}
        for relative in all_files:
            absolute = safe_workspace_path(workspace, str(relative))
            try:
                content = absolute.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            contents[str(relative)] = content

        result = HybridCodeRetriever(
            max_files=self.settings.max_context_files,
            max_context_chars=self.settings.max_context_chars,
        ).retrieve(contents, issue_text=issue_text, search_terms=search_terms)
        return RepositoryContext(
            tree=sorted(str(path) for path in all_files),
            files={hit.path: hit.content for hit in result.hits},
            editable_paths=tuple(hit.path for hit in result.hits),
            evidence=[hit.evidence() for hit in result.hits],
            query_terms=list(result.query_terms),
            strategy=result.strategy,
            candidate_count=result.candidate_count,
            selected_chars=result.selected_chars,
            skipped_for_budget=result.skipped_for_budget,
            max_chars=self.settings.max_context_chars,
        )

    def apply_edits(
        self,
        workspace: Path,
        edits: list[FileEdit],
        *,
        expected_contents: dict[str, str] | None = None,
    ) -> list[str]:
        # Validate the complete proposal before making any edit. The graph has one
        # writer; this additionally catches stale model context at the write boundary.
        # It is not a transaction protocol for arbitrary concurrent filesystem writers.
        prepared: list[tuple[FileEdit, Path, str | None]] = []
        seen: set[Path] = set()
        for edit in edits:
            target = safe_workspace_path(workspace, edit.path)
            if target in seen:
                raise SecurityError(f"Duplicate generated edit: {edit.path}")
            seen.add(target)
            if len(edit.content.encode("utf-8")) > self.settings.max_file_bytes:
                raise SecurityError(f"Generated file is too large: {edit.path}")
            previous = target.read_text(encoding="utf-8") if target.exists() else None
            if expected_contents is not None:
                if edit.path not in expected_contents:
                    raise SecurityError(f"Generated edit is outside supplied context: {edit.path}")
                if previous != expected_contents[edit.path]:
                    raise SecurityError(
                        f"Workspace changed since model context was read: {edit.path}"
                    )
            prepared.append((edit, target, previous))
        changed: list[str] = []
        for edit, target, previous in prepared:
            target.parent.mkdir(parents=True, exist_ok=True)
            if previous != edit.content:
                target.write_text(edit.content, encoding="utf-8")
                changed.append(edit.path)
        return changed

    async def diff(self, workspace: Path) -> str:
        code, stdout, stderr = await run_process(
            "git", "diff", "--no-ext-diff", "--", ".", cwd=workspace
        )
        if code != 0:
            raise RuntimeError(f"Could not calculate git diff: {stderr}")
        return stdout[:100_000]
