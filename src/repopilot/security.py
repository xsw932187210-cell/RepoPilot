from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse


class SecurityError(ValueError):
    """Raised when untrusted input crosses a tool boundary."""


class CapabilityAction(StrEnum):
    READ = "read"
    WRITE = "write"
    CREATE = "create"
    DELETE = "delete"
    RENAME = "rename"


class CapabilityReason(StrEnum):
    READ_DENIED = "read_denied"
    WRITE_DENIED = "write_denied"
    CREATE_DENIED = "create_denied"
    ACTION_NOT_SUPPORTED = "action_not_supported"
    INVALID_PATH = "invalid_path"
    PATH_ALIAS = "path_alias"
    DUPLICATE_PATH = "duplicate_path"
    UNSAFE_LINK = "unsafe_link"
    HARDLINK = "hardlink"
    SPECIAL_FILE = "special_file"
    FILE_TOO_LARGE = "file_too_large"
    OUTSIDE_CONTEXT = "outside_context"
    STALE_CONTENT = "stale_content"
    EXPECTED_ABSENT_REQUIRED = "expected_absent_required"
    TARGET_EXISTS = "target_exists"


class WorkspacePathError(SecurityError):
    """Path normalization or filesystem-shape rejection with a stable reason."""

    def __init__(self, reason: CapabilityReason, message: str) -> None:
        self.reason = reason
        super().__init__(message)


class CapabilityError(SecurityError):
    """Stable policy rejection raised at the repository read/write boundary."""

    def __init__(
        self,
        reason: CapabilityReason,
        action: CapabilityAction,
        path: str,
        message: str,
        *,
        policy_version: str,
    ) -> None:
        self.reason = reason
        self.action = action
        self.path = path
        self.policy_version = policy_version
        super().__init__(f"{reason.value}: {message}")


@dataclass(frozen=True, slots=True)
class NormalizedWorkspacePath:
    value: str
    identity: str

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(self.value.split("/"))


_REPO_PATH = re.compile(r"^/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}$")
_ALLOWED_TEST_COMMANDS = {"python", "python3", "pytest", "ruff"}
_ALLOWED_PYTHON_MODULES = {"pytest", "unittest"}
_DEMO_REPOSITORIES = {"buggy-calculator", "benchmark-suite"}
_SENSITIVE_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"AIza[A-Za-z0-9_-]{25,}"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token)\s*[:=]\s*)[^\s&,;]+"),
    re.compile(r"(?i)([?&](?:api[_-]?key|access[_-]?token|token)=)[^&#\s]+"),
    re.compile(r"(?i)(https?://)[^/@\s]+@"),
]


def validate_repository_url(url: str) -> tuple[str, str]:
    if url.startswith("demo://"):
        demo_name = url.removeprefix("demo://")
        if demo_name not in _DEMO_REPOSITORIES:
            raise SecurityError("Unknown bundled demo repository")
        return "demo", demo_name
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"}:
        raise SecurityError("Only public GitHub HTTPS repositories are accepted")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SecurityError("Credential-bearing or parameterized repository URLs are rejected")
    if not _REPO_PATH.fullmatch(parsed.path):
        raise SecurityError("Repository URL must be https://github.com/<owner>/<repo>")
    owner, repo = parsed.path.removesuffix(".git").strip("/").split("/", maxsplit=1)
    return owner, repo


def validate_branch(branch: str) -> str:
    if not _BRANCH.fullmatch(branch) or ".." in branch or branch.endswith(("/", ".")):
        raise SecurityError("Invalid Git branch name")
    return branch


def normalize_workspace_path(relative_path: str) -> NormalizedWorkspacePath:
    """Return one portable path identity for matching, deduplication, and access."""

    if not relative_path or "\x00" in relative_path:
        raise SecurityError("File path must be a non-empty relative path")
    portable = relative_path.replace("\\", "/")
    if portable.startswith("/") or re.match(r"^[A-Za-z]:", portable):
        raise SecurityError("File path must be relative")
    raw_parts = portable.split("/")
    if ".." in raw_parts:
        raise SecurityError("File path traversal is forbidden")
    parts = tuple(part for part in raw_parts if part not in {"", "."})
    if not parts:
        raise SecurityError("File path must name a file")
    if any(":" in part for part in parts):
        raise SecurityError("File path contains a platform-ambiguous component")
    if any(part.casefold() == ".git" for part in parts):
        raise SecurityError("Direct access to .git is forbidden")
    value = "/".join(parts)
    return NormalizedWorkspacePath(value=value, identity=value.casefold())


def safe_workspace_path(root: Path, relative_path: str) -> Path:
    normalized = normalize_workspace_path(relative_path)
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*normalized.parts)
    current = root_resolved
    for index, part in enumerate(normalized.parts):
        if current.exists():
            matches = [
                entry.name
                for entry in current.iterdir()
                if entry.name.casefold() == part.casefold()
            ]
            if len(matches) > 1 or (matches and part not in matches):
                raise WorkspacePathError(
                    CapabilityReason.PATH_ALIAS,
                    "File path casing does not match the workspace entry",
                )
        current /= part
        if current.is_symlink():
            raise WorkspacePathError(
                CapabilityReason.UNSAFE_LINK,
                "Workspace paths may not traverse symbolic links",
            )
        if current.exists() and index < len(normalized.parts) - 1 and not current.is_dir():
            raise WorkspacePathError(
                CapabilityReason.SPECIAL_FILE,
                "File path traverses a non-directory entry",
            )
    resolved = candidate.resolve(strict=False)
    if resolved == root_resolved or root_resolved not in resolved.parents:
        raise SecurityError("File path escapes the repository workspace")
    return candidate


def parse_test_command(command: str) -> list[str]:
    try:
        parts = shlex.split(command, posix=True)
    except ValueError as exc:
        raise SecurityError("Malformed test command") from exc
    if not parts or parts[0] not in _ALLOWED_TEST_COMMANDS:
        raise SecurityError(f"Test command must start with one of {sorted(_ALLOWED_TEST_COMMANDS)}")
    forbidden = {";", "&&", "||", "|", ">", ">>", "<", "`"}
    if any(
        part in forbidden or "\n" in part or "\r" in part or "\x00" in part
        for part in parts
    ):
        raise SecurityError("Shell control operators are forbidden")
    if parts[0] in {"python", "python3"}:
        if len(parts) < 3 or parts[1] != "-m" or parts[2] not in _ALLOWED_PYTHON_MODULES:
            raise SecurityError(
                "Python test commands must use -m pytest or -m unittest; inline code is forbidden"
            )
    if parts[0] == "ruff" and (len(parts) < 2 or parts[1] != "check"):
        raise SecurityError("Only ruff check is allowed")
    return parts


def redact_secrets(value: str) -> str:
    redacted = value
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted
