from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from repopilot.config import Settings
from repopilot.models import FileEdit
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
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


@dataclass(slots=True)
class RepositoryContext:
    tree: list[str]
    files: dict[str, str]

    def render(self, max_chars: int = 70_000) -> str:
        chunks = ["Repository tree:\n" + "\n".join(self.tree[:200])]
        for path, content in self.files.items():
            chunks.append(f"\n--- {path} ---\n{content}")
        return "\n".join(chunks)[:max_chars]


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
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )
    except TimeoutError:
        process.kill()
        await process.wait()
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

        terms = {term.lower() for term in search_terms if len(term) >= 3}
        terms.update(word.lower() for word in _WORD.findall(issue_text))
        scored: list[tuple[int, Path, str]] = []
        for relative in all_files:
            absolute = safe_workspace_path(workspace, str(relative))
            try:
                content = absolute.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            haystack = f"{relative}\n{content}".lower()
            score = sum(
                3 if term in str(relative).lower() else 1
                for term in terms
                if term in haystack
            )
            if relative.name.lower().startswith("test") or "tests" in relative.parts:
                score += 1
            scored.append((score, relative, content))

        scored.sort(key=lambda item: (-item[0], str(item[1])))
        selected = scored[: self.settings.max_context_files]
        return RepositoryContext(
            tree=sorted(str(path) for path in all_files),
            files={str(path): content for _, path, content in selected},
        )

    def apply_edits(self, workspace: Path, edits: list[FileEdit]) -> list[str]:
        changed: list[str] = []
        for edit in edits:
            target = safe_workspace_path(workspace, edit.path)
            encoded = edit.content.encode("utf-8")
            if len(encoded) > self.settings.max_file_bytes:
                raise SecurityError(f"Generated file is too large: {edit.path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            previous = target.read_text(encoding="utf-8") if target.exists() else None
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
