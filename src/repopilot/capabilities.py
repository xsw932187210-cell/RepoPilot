from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from repopilot.security import (
    CapabilityAction,
    CapabilityError,
    CapabilityReason,
    NormalizedWorkspacePath,
    normalize_workspace_path,
)

CAPABILITY_POLICY_VERSION = "workspace-capabilities-v1"

_CREDENTIAL_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "id_rsa",
    "id_ed25519",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "*credentials*",
    "*secrets*",
)
_TEST_PATTERNS = (
    "tests/*",
    "test/*",
    "*/tests/*",
    "*/test/*",
    "__tests__/*",
    "*/__tests__/*",
    "test_*.py",
    "*_test.py",
    "*.test.*",
    "*.spec.*",
)
_BUILD_AND_CI_PATTERNS = (
    ".github/*",
    ".gitlab-ci.yml",
    ".circleci/*",
    "azure-pipelines.yml",
    "Jenkinsfile",
    "Dockerfile*",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "compose*.yml",
    "compose*.yaml",
    "Makefile",
    "makefile",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "noxfile.py",
    "requirements*.txt",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    ".pre-commit-config.yaml",
)
_SOURCE_AND_DOC_PATTERNS = (
    "*.c",
    "*.cc",
    "*.cpp",
    "*.css",
    "*.go",
    "*.h",
    "*.hpp",
    "*.html",
    "*.java",
    "*.js",
    "*.jsx",
    "*.md",
    "*.py",
    "*.rs",
    "*.sql",
    "*.ts",
    "*.tsx",
)


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    identity = path.casefold()
    name = PurePosixPath(identity).name
    return any(
        fnmatchcase(identity, pattern.casefold())
        or ("/" not in pattern and fnmatchcase(name, pattern.casefold()))
        for pattern in patterns
    )


class PathCapability(BaseModel):
    model_config = ConfigDict(frozen=True)

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    @field_validator("allow", "deny")
    @classmethod
    def validate_patterns(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        if any(not pattern or "\x00" in pattern for pattern in patterns):
            raise ValueError("Capability patterns must be non-empty")
        return patterns

    def permits(self, path: NormalizedWorkspacePath) -> bool:
        return _matches(path.value, self.allow) and not _matches(path.value, self.deny)


class CreateCapability(BaseModel):
    model_config = ConfigDict(frozen=True)

    directories: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    max_bytes: int = Field(default=0, ge=0)

    @field_validator("directories")
    @classmethod
    def normalize_directories(cls, directories: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for directory in directories:
            if directory in {"", "."}:
                normalized.append("")
                continue
            normalized.append(
                normalize_workspace_path(f"{directory}/placeholder").value.rsplit("/", 1)[0]
            )
        return tuple(normalized)

    @field_validator("extensions")
    @classmethod
    def normalize_extensions(cls, extensions: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(extension.casefold() for extension in extensions)
        if any(not extension.startswith(".") or "/" in extension for extension in normalized):
            raise ValueError("Create extensions must be suffixes such as '.py'")
        return normalized

    def permits(self, path: NormalizedWorkspacePath, size: int) -> bool:
        parent = PurePosixPath(path.value).parent.as_posix()
        parent = "" if parent == "." else parent
        in_directory = any(
            (not directory and not parent)
            or parent == directory
            or (bool(directory) and parent.startswith(f"{directory}/"))
            for directory in self.directories
        )
        extension_allowed = PurePosixPath(path.value).suffix.casefold() in self.extensions
        return in_directory and extension_allowed and size <= self.max_bytes


class WorkspaceCapabilityPolicy(BaseModel):
    """Trusted, versioned file capability policy.

    Policy objects are constructed by the service or a trusted project adapter. Issue text,
    repository files, and model output supply requested paths only; they cannot replace this
    policy or add rules to it.
    """

    model_config = ConfigDict(frozen=True)

    version: Literal["workspace-capabilities-v1"] = CAPABILITY_POLICY_VERSION
    read: PathCapability
    write: PathCapability
    create: CreateCapability = Field(default_factory=CreateCapability)

    def permits(self, action: CapabilityAction, path: str) -> bool:
        normalized = normalize_workspace_path(path)
        if action is CapabilityAction.READ:
            return self.read.permits(normalized)
        if action is CapabilityAction.WRITE:
            return self.write.permits(normalized)
        return False

    def require(
        self,
        action: CapabilityAction,
        path: NormalizedWorkspacePath,
        *,
        size: int = 0,
        expected_absent: bool = False,
    ) -> None:
        if action in {CapabilityAction.DELETE, CapabilityAction.RENAME}:
            raise CapabilityError(
                CapabilityReason.ACTION_NOT_SUPPORTED,
                action,
                path.value,
                f"{action.value} is not supported by policy {self.version}",
                policy_version=self.version,
            )
        if action is CapabilityAction.READ and not self.read.permits(path):
            raise CapabilityError(
                CapabilityReason.READ_DENIED,
                action,
                path.value,
                "path is not readable under the trusted workspace policy",
                policy_version=self.version,
            )
        if action is CapabilityAction.WRITE and not self.write.permits(path):
            raise CapabilityError(
                CapabilityReason.WRITE_DENIED,
                action,
                path.value,
                "path is read-only under the trusted workspace policy",
                policy_version=self.version,
            )
        if action is CapabilityAction.CREATE:
            if not expected_absent:
                raise CapabilityError(
                    CapabilityReason.EXPECTED_ABSENT_REQUIRED,
                    action,
                    path.value,
                    "creation requires an explicit expected-absent precondition",
                    policy_version=self.version,
                )
            if not self.create.permits(path, size):
                raise CapabilityError(
                    CapabilityReason.CREATE_DENIED,
                    action,
                    path.value,
                    "directory, extension, or byte limit is not authorized for creation",
                    policy_version=self.version,
                )


def default_workspace_policy(
    *, max_file_bytes: int, allow_test_writes: bool = False
) -> WorkspaceCapabilityPolicy:
    write_deny = (*_CREDENTIAL_PATTERNS, *_BUILD_AND_CI_PATTERNS)
    if not allow_test_writes:
        write_deny = (*write_deny, *_TEST_PATTERNS)
    return WorkspaceCapabilityPolicy(
        read=PathCapability(allow=("*",), deny=_CREDENTIAL_PATTERNS),
        write=PathCapability(allow=_SOURCE_AND_DOC_PATTERNS, deny=write_deny),
        create=CreateCapability(max_bytes=max_file_bytes),
    )


def real_evaluation_policy(*, max_file_bytes: int) -> WorkspaceCapabilityPolicy:
    """Keep the pinned corpus stricter than the generic application policy."""

    return WorkspaceCapabilityPolicy(
        read=PathCapability(allow=("*",), deny=_CREDENTIAL_PATTERNS),
        write=PathCapability(
            allow=("thefuck/*.py",),
            deny=_CREDENTIAL_PATTERNS,
        ),
        create=CreateCapability(max_bytes=max_file_bytes),
    )
