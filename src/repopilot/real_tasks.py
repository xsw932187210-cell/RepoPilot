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

from repopilot.capabilities import WorkspaceCapabilityPolicy, real_evaluation_policy
from repopilot.data_boundary import require_secret_free
from repopilot.security import CapabilityAction, normalize_workspace_path, safe_workspace_path

MANIFEST_SCHEMA_VERSION = 2
PROJECT_ADAPTER_VERSION = "python-source-v1"
VerificationStatus = Literal["assembled", "reproduction_verified"]
RunCommand = Callable[..., subprocess.CompletedProcess[bytes]]


@dataclass(frozen=True)
class RuntimeEnvironment:
    image: str
    python_version: str
    pythonpath: tuple[str, ...]
    base_image: str
    recipe: str
    recipe_sha256: str
    preparation: str
    install_argv: tuple[tuple[str, ...], ...]

    @property
    def container_pythonpath(self) -> tuple[str, ...]:
        return tuple(
            "/workspace" if root == "." else f"/workspace/{root}" for root in self.pythonpath
        )


@dataclass(frozen=True)
class ProjectAdapter:
    project: str
    version: str
    source_roots: tuple[str, ...]
    readonly_paths: tuple[str, ...]
    test_roots: tuple[str, ...]
    visible_test_policy: str
    acceptance_test_policy: str
    runtime: RuntimeEnvironment
    fingerprint: str

    def capability_policy(self, max_file_bytes: int) -> WorkspaceCapabilityPolicy:
        return real_evaluation_policy(
            max_file_bytes=max_file_bytes,
            source_roots=self.source_roots,
            readonly_paths=self.readonly_paths,
        )


@dataclass(frozen=True)
class CorpusSource:
    name: str
    url: str
    revision: str
    case: str
    kind: Literal["real", "synthetic"] = "real"


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
    adapter: ProjectAdapter | None = None


@dataclass(frozen=True)
class RealTaskManifest:
    schema_version: int
    tasks: tuple[RealTask, ...]
    projects: tuple[ProjectAdapter, ...] = ()
    read_only: bool = False

    def by_id(self, task_id: str) -> RealTask:
        matches = [task for task in self.tasks if task.id == task_id]
        if not matches:
            raise KeyError(f"Unknown real task: {task_id}")
        return matches[0]

    def require_executable(self) -> None:
        if self.read_only or self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                "Legacy manifest is read-only; migrate to schema 2 in a new experiment"
            )

    def adapter_for(self, project: str) -> ProjectAdapter:
        self.require_executable()
        for adapter in self.projects:
            if adapter.project == project:
                return adapter
        raise ValueError(f"Project has no trusted adapter: {project}")


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _safe_relative_path(value: str, field: str) -> PurePosixPath:
    value = _require_string(value, field)
    if (
        "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(char in value for char in "\x00\r\n:*?[]")
    ):
        raise ValueError(f"{field} must be a contained relative path: {value!r}")
    try:
        return PurePosixPath(normalize_workspace_path(value).value)
    except ValueError as error:
        raise ValueError(f"{field} must be a contained relative path") from error


def _require_sha256(value: Any, field: str) -> str:
    value = _require_string(value, field)
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def _require_id(value: Any, field: str) -> str:
    value = _require_string(value, field)
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", value) is None:
        raise ValueError(f"{field} must be a portable lowercase identifier")
    return value


def _path_list(raw: Any, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{field} must be a non-empty path list")
    paths = tuple(str(_safe_relative_path(value, field)) for value in raw)
    if len({path.casefold() for path in paths}) != len(paths):
        raise ValueError(f"{field} contains duplicate path identities")
    return paths


def _within(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in roots)


def _immutable_image(value: Any, field: str) -> str:
    value = _require_string(value, field)
    if re.fullmatch(r"(?:sha256:|[^\s@]+@sha256:)[0-9a-f]{64}", value) is None:
        raise ValueError(f"{field} must identify an immutable image by SHA-256")
    return value


def _parse_adapter(project: str, raw: dict[str, Any]) -> ProjectAdapter:
    project = _require_id(project, "project")
    if raw.get("version") != PROJECT_ADAPTER_VERSION:
        raise ValueError("Unsupported project adapter version")
    fingerprint = _require_sha256(raw.get("fingerprint"), "project.fingerprint")
    if task_fingerprint(raw) != fingerprint:
        raise ValueError("Project adapter fingerprint mismatch")
    source_roots = _path_list(raw.get("source_roots"), "source_roots")
    readonly_paths = _path_list(raw.get("readonly_paths"), "readonly_paths")
    test_roots = _path_list(raw.get("test_roots"), "test_roots")
    if not all(_within(root, readonly_paths) for root in test_roots):
        raise ValueError("Every test root must be explicitly read-only")
    if raw.get("visible_test_policy") != "buggy-tree-only-v1":
        raise ValueError("Unsupported visible test policy")
    if raw.get("acceptance_test_policy") != "withheld-overlay-v1":
        raise ValueError("Unsupported acceptance test policy")
    runtime = raw.get("runtime")
    if not isinstance(runtime, dict):
        raise ValueError("Project runtime information is required")
    python_version = _require_string(runtime.get("python_version"), "runtime.python_version")
    if re.fullmatch(r"3\.[0-9]+", python_version) is None:
        raise ValueError("runtime.python_version must be a Python 3 major.minor")
    pythonpath_raw = runtime.get("pythonpath")
    if not isinstance(pythonpath_raw, list) or not pythonpath_raw:
        raise ValueError("runtime.pythonpath must be an explicit non-empty path list")
    pythonpath = tuple(
        "." if root == "." else str(_safe_relative_path(root, "runtime.pythonpath"))
        for root in pythonpath_raw
    )
    install = runtime.get("install_argv")
    if not isinstance(install, list) or not install:
        raise ValueError("runtime.install_argv must document trusted dependency preparation")
    commands: list[tuple[str, ...]] = []
    for command in install:
        if not isinstance(command, list) or not command:
            raise ValueError("Each trusted install command must be a non-empty argv list")
        commands.append(tuple(_require_string(part, "runtime.install_argv") for part in command))
    return ProjectAdapter(
        project=project,
        version=PROJECT_ADAPTER_VERSION,
        source_roots=source_roots,
        readonly_paths=readonly_paths,
        test_roots=test_roots,
        visible_test_policy=raw["visible_test_policy"],
        acceptance_test_policy=raw["acceptance_test_policy"],
        runtime=RuntimeEnvironment(
            image=_immutable_image(runtime.get("image"), "runtime.image"),
            python_version=python_version,
            pythonpath=pythonpath,
            base_image=_immutable_image(runtime.get("base_image"), "runtime.base_image"),
            recipe=str(_safe_relative_path(runtime.get("recipe"), "runtime.recipe")),
            recipe_sha256=_require_sha256(runtime.get("recipe_sha256"), "runtime.recipe_sha256"),
            preparation=_require_string(runtime.get("preparation"), "runtime.preparation"),
            install_argv=tuple(commands),
        ),
        fingerprint=fingerprint,
    )


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
    if not isinstance(raw, list):
        raise ValueError(f"{field} must be an argv list")
    argv = tuple(_require_string(part, field) for part in raw)
    if len(argv) < 3 or argv[:3] != ("python", "-m", "pytest"):
        raise ValueError(f"{field} must invoke pytest via ['python', '-m', 'pytest', ...]")
    return argv


def _parse_task(raw: dict[str, Any], adapter: ProjectAdapter | None = None) -> RealTask:
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
    corpus_kind = corpus_raw.get("kind", "real")
    if corpus_kind not in {"real", "synthetic"}:
        raise ValueError("Corpus kind must be real or synthetic")
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
    if not overlays:
        raise ValueError("At least one evaluator-owned hidden overlay is required")
    if len({item.destination.casefold() for item in overlays}) != len(overlays):
        raise ValueError("Hidden overlay destinations must be unique")

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

    if adapter is not None:
        if source_raw.get("buggy_commit") == source_raw.get("fixed_commit"):
            raise ValueError("Buggy and fixed commits must be distinct")
        for argv in (visible_argv, acceptance_argv):
            selectors = 0
            for argument in argv[3:]:
                if argument in {
                    "-q",
                    "-v",
                    "-vv",
                    "-x",
                    "--disable-warnings",
                    "--tb=short",
                    "--tb=no",
                }:
                    continue
                selected = str(_safe_relative_path(argument.split("::", 1)[0], "pytest selector"))
                if not selected.endswith(".py") or not _within(selected, adapter.test_roots):
                    raise ValueError("Pytest must select explicit files within trusted test roots")
                if any(char in argument for char in "\r\n\x00"):
                    raise ValueError("Unsafe pytest selector")
                selectors += 1
            if not selectors:
                raise ValueError("Pytest must select at least one explicit test file")
        for overlay in overlays:
            if not _within(overlay.destination, adapter.test_roots):
                raise ValueError("Hidden overlay destination must be within trusted test roots")
            prefix = f"overlays/{raw['id']}/{overlay.destination}"
            if overlay.source != prefix:
                raise ValueError("Hidden overlay source must match its task and test destination")
        if raw.get("python_version") != adapter.runtime.python_version:
            raise ValueError("Task Python version differs from its project runtime")
        policy = adapter.capability_policy(max_file_bytes=120_000)
        if not all(policy.permits(CapabilityAction.WRITE, path) for path in expected_fix_files):
            raise ValueError(
                "Reference source paths must satisfy the trusted project source policy"
            )

    return RealTask(
        id=_require_id(raw["id"], "id"),
        project=_require_id(raw["project"], "project"),
        corpus=CorpusSource(
            name=_require_string(corpus_raw["name"], "corpus.name"),
            url=_require_string(corpus_raw["url"], "corpus.url"),
            revision=_require_git_oid(corpus_raw["revision"], "corpus.revision"),
            case=_require_string(corpus_raw["case"], "corpus.case"),
            kind=corpus_kind,
        ),
        source=SourceRevision(
            repository_url=_require_string(source_raw["repository_url"], "source.url"),
            buggy_commit=_require_git_oid(source_raw["buggy_commit"], "source.buggy_commit"),
            fixed_commit=_require_git_oid(source_raw["fixed_commit"], "source.fixed_commit"),
            license_spdx=_require_string(source_raw.get("license_spdx"), "source.license_spdx"),
            license_file=str(
                _safe_relative_path(source_raw.get("license_file"), "source.license_file")
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
        adapter=adapter,
    )


def load_real_task_manifest(path: Path, *, allow_legacy_read: bool = False) -> RealTaskManifest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    require_secret_free(raw)
    schema = raw.get("schema_version")
    if schema == 1 and allow_legacy_read:
        return RealTaskManifest(
            schema_version=1,
            tasks=tuple(_parse_task(item) for item in raw["tasks"]),
            read_only=True,
        )
    if schema != MANIFEST_SCHEMA_VERSION:
        raise ValueError(f"Unsupported real-task schema version: {raw.get('schema_version')!r}")
    projects_raw = raw.get("projects")
    if not isinstance(projects_raw, dict) or not projects_raw:
        raise ValueError("Manifest must declare trusted project adapters")
    adapters = {project: _parse_adapter(project, item) for project, item in projects_raw.items()}
    if not isinstance(raw.get("tasks"), list) or not raw["tasks"]:
        raise ValueError("Manifest tasks must be a non-empty list")
    tasks_list = []
    for item in raw["tasks"]:
        project = item.get("project")
        if project not in adapters:
            raise ValueError("Task references an undeclared project adapter")
        tasks_list.append(_parse_task(item, adapters[project]))
    tasks = tuple(tasks_list)
    task_ids = [task.id for task in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("Real-task ids must be unique")
    manifest = RealTaskManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        tasks=tasks,
        projects=tuple(adapters.values()),
    )
    # All evaluator-owned assets must match the manifest before a model can run.
    # This directory is trusted configuration, never the candidate checkout.
    for adapter in manifest.projects:
        recipe = safe_workspace_path(path.parent, adapter.runtime.recipe)
        if not recipe.is_file() or _sha256_file(recipe) != adapter.runtime.recipe_sha256:
            raise ValueError("Runtime build recipe checksum mismatch or missing recipe")
    for task in manifest.tasks:
        for overlay in task.test.hidden_overlay:
            asset = safe_workspace_path(path.parent, overlay.source)
            if not asset.is_file() or _sha256_file(asset) != overlay.sha256:
                raise ValueError("Hidden overlay checksum mismatch or missing fixture")
    return manifest


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
    if task.adapter is None:
        raise ValueError("Legacy task metadata is read-only; no executable project adapter")
    workspace = export_git_tree(
        source_repository,
        task.source.buggy_commit,
        destination,
        runner=runner,
    )
    verify_source_license(task, workspace)
    for root in task.adapter.source_roots:
        source_root = safe_workspace_path(workspace, root)
        if not source_root.is_dir():
            raise ValueError("Declared project source root is absent")
    return workspace


def verify_source_license(task: RealTask, workspace: Path) -> None:
    license_path = safe_workspace_path(workspace, task.source.license_file)
    if not license_path.is_file() or not license_path.read_bytes().strip():
        raise ValueError("Declared source license file must exist and be non-empty")


def prepare_reference_workspace(
    task: RealTask,
    source_repository: Path,
    destination: Path,
    *,
    runner: RunCommand = subprocess.run,
) -> Path:
    """Evaluator-only fixed tree. Never expose this path to an agent or model."""
    if task.adapter is None:
        raise ValueError("Legacy task metadata is read-only; no executable project adapter")
    workspace = export_git_tree(
        source_repository,
        task.source.fixed_commit,
        destination,
        runner=runner,
    )
    verify_source_license(task, workspace)
    return workspace


def materialize_hidden_overlay(
    task: RealTask,
    corpus_root: Path,
    destination: Path,
) -> tuple[Path, ...]:
    """Copy hidden tests into an evaluator-only export after candidate generation."""
    if task.adapter is None:
        raise ValueError("Legacy task metadata is read-only; no executable project adapter")
    corpus_root = corpus_root.resolve(strict=True)
    destination = destination.resolve(strict=True)
    written: list[Path] = []
    for overlay in task.test.hidden_overlay:
        source_rel = _safe_relative_path(overlay.source, "overlay.source")
        target_rel = _safe_relative_path(overlay.destination, "overlay.destination")
        if overlay.source != f"overlays/{task.id}/{overlay.destination}":
            raise ValueError("Overlay source must match its task and test destination")
        if not _within(overlay.destination, task.adapter.test_roots):
            raise ValueError("Overlay destination must be within trusted test roots")
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
    "PROJECT_ADAPTER_VERSION",
    "ProjectAdapter",
    "RuntimeEnvironment",
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
    "verify_source_license",
]
