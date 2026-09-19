from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

MANIFEST_SCHEMA_VERSION = 1
VerificationStatus = Literal["assembled", "reproduction_verified"]
RunCommand = Callable[..., subprocess.CompletedProcess[bytes]]


@dataclass(frozen=True)
class CorpusSource:
    name: str
    url: str
    revision: str
    case: str


@dataclass(frozen=True)
class SourceRevision:
    repository_url: str
    buggy_commit: str
    fixed_commit: str
    license_spdx: str
    license_file: str


@dataclass(frozen=True)
class Issue:
    title: str
    body: str


@dataclass(frozen=True)
class HiddenOverlayFile:
    source: str
    destination: str
    sha256: str
    replaces_sha256: str


@dataclass(frozen=True)
class TestRecipe:
    visible_argv: tuple[str, ...]
    acceptance_argv: tuple[str, ...]
    hidden_overlay: tuple[HiddenOverlayFile, ...]


@dataclass(frozen=True)
class Verification:
    status: VerificationStatus
    details: str


@dataclass(frozen=True)
class RealTask:
    id: str
    project: str
    corpus: CorpusSource
    source: SourceRevision
    python_version: str
    issue: Issue
    test: TestRecipe
    expected_fix_files: tuple[str, ...]
    verification: Verification
    fingerprint: str


@dataclass(frozen=True)
class RealTaskManifest:
    schema_version: int
    dependency_image: str
    dependency_install_argv: tuple[tuple[str, ...], ...]
    tasks: tuple[RealTask, ...]

    def by_id(self, task_id: str) -> RealTask:
        matches = [task for task in self.tasks if task.id == task_id]
        if not matches:
            raise KeyError(f"Unknown real task: {task_id}")
        return matches[0]


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _safe_relative_path(value: str, field: str) -> PurePosixPath:
    path = PurePosixPath(_require_string(value, field))
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"{field} must be a contained relative path: {value!r}")
    return path


def _require_git_oid(value: Any, field: str) -> str:
    oid = _require_string(value, field)
    if re.fullmatch(r"[0-9a-f]{40}", oid) is None:
        raise ValueError(f"{field} must be a full lowercase 40-character Git object id")
    return oid


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _task_fingerprint_payload(raw: dict[str, Any]) -> dict[str, Any]:
    return {key: raw[key] for key in sorted(raw) if key != "fingerprint"}


def task_fingerprint(raw: dict[str, Any]) -> str:
    payload = json.dumps(
        _task_fingerprint_payload(raw),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _parse_pytest_argv(raw: Any, field: str) -> tuple[str, ...]:
    argv = tuple(_require_string(part, field) for part in raw)
    if len(argv) < 3 or argv[:3] != ("python", "-m", "pytest"):
        raise ValueError(f"{field} must invoke pytest via ['python', '-m', 'pytest', ...]")
    return argv


def _parse_task(raw: dict[str, Any]) -> RealTask:
    actual_fingerprint = _require_string(raw.get("fingerprint"), "fingerprint")
    expected_fingerprint = task_fingerprint(raw)
    if actual_fingerprint != expected_fingerprint:
        raise ValueError(
            f"Task {raw.get('id', '<unknown>')} fingerprint mismatch: "
            f"expected {expected_fingerprint}, found {actual_fingerprint}"
        )

    corpus_raw = raw["corpus"]
    source_raw = raw["source"]
    issue_raw = raw["issue"]
    test_raw = raw["test"]
    verification_raw = raw["verification"]
    overlays: list[HiddenOverlayFile] = []
    for index, overlay in enumerate(test_raw["hidden_overlay"]):
        source = str(_safe_relative_path(overlay["source"], f"overlay[{index}].source"))
        destination = str(
            _safe_relative_path(overlay["destination"], f"overlay[{index}].destination")
        )
        sha256 = _require_string(overlay["sha256"], f"overlay[{index}].sha256")
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise ValueError(f"overlay[{index}].sha256 must be a lowercase SHA-256")
        replaces_sha256 = _require_string(
            overlay["replaces_sha256"], f"overlay[{index}].replaces_sha256"
        )
        if len(replaces_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in replaces_sha256
        ):
            raise ValueError(f"overlay[{index}].replaces_sha256 must be a lowercase SHA-256")
        overlays.append(HiddenOverlayFile(source, destination, sha256, replaces_sha256))

    visible_argv = _parse_pytest_argv(test_raw["visible_argv"], "test.visible_argv")
    acceptance_argv = _parse_pytest_argv(test_raw["acceptance_argv"], "test.acceptance_argv")
    if visible_argv == acceptance_argv:
        raise ValueError("visible and acceptance pytest commands must be distinct")
    for overlay in overlays:
        if any(
            argument == overlay.destination or argument.startswith(f"{overlay.destination}::")
            for argument in visible_argv
        ):
            raise ValueError("visible pytest command may not reference a hidden overlay")
        if not any(
            argument == overlay.destination or argument.startswith(f"{overlay.destination}::")
            for argument in acceptance_argv
        ):
            raise ValueError("acceptance pytest command must reference its hidden overlay")
    status = verification_raw["status"]
    if status not in {"assembled", "reproduction_verified"}:
        raise ValueError(f"Unsupported verification status: {status!r}")

    expected_fix_files = tuple(
        str(_safe_relative_path(path, "expected_fix_files")) for path in raw["expected_fix_files"]
    )
    if not expected_fix_files:
        raise ValueError("expected_fix_files must not be empty")

    return RealTask(
        id=_require_string(raw["id"], "id"),
        project=_require_string(raw["project"], "project"),
        corpus=CorpusSource(
            name=_require_string(corpus_raw["name"], "corpus.name"),
            url=_require_string(corpus_raw["url"], "corpus.url"),
            revision=_require_git_oid(corpus_raw["revision"], "corpus.revision"),
            case=_require_string(corpus_raw["case"], "corpus.case"),
        ),
        source=SourceRevision(
            repository_url=_require_string(source_raw["repository_url"], "source.url"),
            buggy_commit=_require_git_oid(source_raw["buggy_commit"], "source.buggy_commit"),
            fixed_commit=_require_git_oid(source_raw["fixed_commit"], "source.fixed_commit"),
            license_spdx=_require_string(source_raw["license_spdx"], "source.license_spdx"),
            license_file=str(
                _safe_relative_path(source_raw["license_file"], "source.license_file")
            ),
        ),
        python_version=_require_string(raw["python_version"], "python_version"),
        issue=Issue(
            title=_require_string(issue_raw["title"], "issue.title"),
            body=_require_string(issue_raw["body"], "issue.body"),
        ),
        test=TestRecipe(
            visible_argv=visible_argv,
            acceptance_argv=acceptance_argv,
            hidden_overlay=tuple(overlays),
        ),
        expected_fix_files=expected_fix_files,
        verification=Verification(
            status=status,
            details=_require_string(verification_raw["details"], "verification.details"),
        ),
        fingerprint=actual_fingerprint,
    )


def load_real_task_manifest(path: Path) -> RealTaskManifest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError(f"Unsupported real-task schema version: {raw.get('schema_version')!r}")
    tasks = tuple(_parse_task(item) for item in raw["tasks"])
    task_ids = [task.id for task in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("Real-task ids must be unique")
    install_argv = tuple(
        tuple(_require_string(part, "dependency_install_argv") for part in command)
        for command in raw["environment"]["install_argv"]
    )
    return RealTaskManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        dependency_image=_require_string(raw["environment"]["image"], "environment.image"),
        dependency_install_argv=install_argv,
        tasks=tasks,
    )


def _prepare_empty_destination(destination: Path) -> Path:
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"Destination must be absent or empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def export_git_tree(
    repository: Path,
    revision: str,
    destination: Path,
    *,
    runner: RunCommand = subprocess.run,
) -> Path:
    """Export one commit without Git history or evaluator-only benchmark files."""
    repository = repository.resolve(strict=True)
    revision = _require_git_oid(revision, "revision")
    if not (repository / ".git").exists():
        raise ValueError(f"Source repository is not a Git checkout: {repository}")
    destination = _prepare_empty_destination(destination)
    completed = runner(
        ["git", "-C", str(repository), "archive", "--format=tar", revision],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    archive = completed.stdout
    if not isinstance(archive, bytes):
        raise TypeError("Git archive runner must return bytes on stdout")

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
        for member in stream.getmembers():
            relative = _safe_relative_path(member.name.rstrip("/"), "archive member")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError(
                    f"Archive contains unsupported link or special file: {member.name}"
                )
            source = stream.extractfile(member)
            if source is None:
                raise ValueError(f"Archive member has no content: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(member.mode & 0o777)

    assert_agent_workspace_clean(destination)
    return destination


def prepare_agent_workspace(
    task: RealTask,
    source_repository: Path,
    destination: Path,
    *,
    runner: RunCommand = subprocess.run,
) -> Path:
    return export_git_tree(
        source_repository,
        task.source.buggy_commit,
        destination,
        runner=runner,
    )


def prepare_reference_workspace(
    task: RealTask,
    source_repository: Path,
    destination: Path,
    *,
    runner: RunCommand = subprocess.run,
) -> Path:
    """Evaluator-only fixed tree. Never expose this path to an agent or model."""
    return export_git_tree(
        source_repository,
        task.source.fixed_commit,
        destination,
        runner=runner,
    )


def materialize_hidden_overlay(
    task: RealTask,
    corpus_root: Path,
    destination: Path,
) -> tuple[Path, ...]:
    """Copy fixed-version tests into a completed agent workspace for evaluation."""
    corpus_root = corpus_root.resolve(strict=True)
    destination = destination.resolve(strict=True)
    written: list[Path] = []
    for overlay in task.test.hidden_overlay:
        source_rel = _safe_relative_path(overlay.source, "overlay.source")
        target_rel = _safe_relative_path(overlay.destination, "overlay.destination")
        expected_source_prefix = PurePosixPath("overlays", task.id, "tests")
        if not source_rel.is_relative_to(expected_source_prefix):
            raise ValueError(
                f"Overlay source must be within {expected_source_prefix}: {overlay.source}"
            )
        if not target_rel.parts or target_rel.parts[0] != "tests":
            raise ValueError(f"Overlay destination must be within tests/: {overlay.destination}")
        lexical_source = corpus_root.joinpath(*source_rel.parts)
        if lexical_source.is_symlink():
            raise ValueError(f"Overlay source may not be a symlink: {overlay.source}")
        source = lexical_source.resolve(strict=True)
        if not source.is_relative_to(corpus_root):
            raise ValueError(f"Overlay source escapes corpus root: {overlay.source}")
        if _sha256_file(source) != overlay.sha256:
            raise ValueError(f"Overlay checksum mismatch: {overlay.source}")
        target = destination.joinpath(*target_rel.parts)
        target_parent = target.parent.resolve(strict=True)
        if not target_parent.is_relative_to(destination):
            raise ValueError(f"Overlay destination escapes workspace: {overlay.destination}")
        if target.is_symlink():
            raise ValueError(f"Overlay destination is a symlink: {overlay.destination}")
        if not target.is_file():
            raise ValueError(
                f"Overlay destination must be an existing regular file: {overlay.destination}"
            )
        current_sha256 = _sha256_file(target)
        if current_sha256 == overlay.sha256:
            # A fixed reference tree already contains the evaluator-owned test bytes.
            written.append(target)
            continue
        if current_sha256 != overlay.replaces_sha256:
            raise ValueError(f"Overlay replacement checksum mismatch: {overlay.destination}")
        original_mode = target.stat().st_mode & 0o777
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as output, source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
                output.flush()
                os.fsync(output.fileno())
            temporary.chmod(original_mode)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        written.append(target)
    return tuple(written)


def assert_agent_workspace_clean(workspace: Path) -> None:
    workspace = workspace.resolve(strict=True)
    forbidden_names = {
        ".git",
        "bug_patch.txt",
        "bugsinpy_bug.info",
        "bugsinpy_patchfile.info",
        "bugsinpy_requirements.txt",
        "bugsinpy_run_test.sh",
        "bugsinpy_setup.sh",
    }
    leaked = [
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
        if path.name in forbidden_names
    ]
    if leaked:
        raise ValueError(f"Agent workspace contains evaluator-only artifacts: {sorted(leaked)}")


__all__ = [
    "CorpusSource",
    "HiddenOverlayFile",
    "Issue",
    "MANIFEST_SCHEMA_VERSION",
    "RealTask",
    "RealTaskManifest",
    "SourceRevision",
    "TestRecipe",
    "Verification",
    "assert_agent_workspace_clean",
    "export_git_tree",
    "load_real_task_manifest",
    "materialize_hidden_overlay",
    "prepare_agent_workspace",
    "prepare_reference_workspace",
    "task_fingerprint",
]
