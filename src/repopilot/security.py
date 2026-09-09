from __future__ import annotations

import re
import shlex
from pathlib import Path
from urllib.parse import urlparse


class SecurityError(ValueError):
    """Raised when untrusted input crosses a tool boundary."""


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


def safe_workspace_path(root: Path, relative_path: str) -> Path:
    if not relative_path or Path(relative_path).is_absolute() or "\x00" in relative_path:
        raise SecurityError("File path must be relative")
    root_resolved = root.resolve()
    candidate = (root_resolved / relative_path).resolve()
    if candidate == root_resolved or root_resolved not in candidate.parents:
        raise SecurityError("File path escapes the repository workspace")
    if ".git" in candidate.relative_to(root_resolved).parts:
        raise SecurityError("Direct writes to .git are forbidden")
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
