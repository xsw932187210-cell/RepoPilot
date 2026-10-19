"""Content-addressed repository artifacts, independent of a mutable workspace.

Only repository contents are captured (.git metadata is excluded). Reads verify both
the manifest and every archived byte. Test runners receive disposable materializations;
they never receive the stored artifact itself. CH-05A can reuse this interface for staging.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from repopilot.security import SecurityError, normalize_workspace_path

MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
MAX_ARTIFACT_ENTRIES = 10_000
_SHA = re.compile(r"^[0-9a-f]{64}$")


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "candidate-v1"
    candidate_id: str
    task_id: str
    generation: int
    base_sha: str
    artifact_digest: str
    artifact_ref: str
    policy_version: str
    diff_digest: str
    manifest: dict[str, Any]


def candidate_identity(candidate: Candidate) -> str:
    return digest_json(
        {
            key: getattr(candidate, key)
            for key in (
                "task_id",
                "generation",
                "base_sha",
                "artifact_digest",
                "policy_version",
                "diff_digest",
            )
        }
    )


def _path(raw: str) -> str:
    normalized = normalize_workspace_path(raw)
    if normalized.value != raw:
        raise SecurityError("Artifact paths must be canonical")
    return raw


def _git(workspace: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(  # noqa: S603 -- argument array, no shell or model command.
        ["git", *args],  # noqa: S607 -- same trusted PATH used by repository preparation.
        cwd=workspace,
        capture_output=True,
        timeout=120,
        input=input_bytes,
        env={
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "LC_ALL": "C",
        },
    )
    if result.returncode not in ({0, 1} if args[0] == "diff" else {0}):
        raise SecurityError("Could not generate the complete candidate diff")
    return result.stdout


def _write_base(workspace: Path, base_sha: str, root: Path) -> None:
    # git archive honors export-ignore/export-subst and can silently omit or
    # rewrite tracked bytes. Read the actual commit tree/blobs instead.
    records = _git(workspace, "ls-tree", "-rlz", "--full-tree", base_sha).split(b"\0")
    blobs: list[tuple[str, str, int, int]] = []
    total = 0
    for record in filter(None, records):
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, sha, size = metadata.split()
        if kind != b"blob" or mode not in {b"100644", b"100755"}:
            raise SecurityError("Base tree contains unsupported links or entries")
        path, length = _path(raw_path.decode("utf-8")), int(size)
        total += length
        if total > MAX_ARTIFACT_BYTES or len(blobs) >= MAX_ARTIFACT_ENTRIES:
            raise SecurityError("Base tree exceeds artifact limits")
        blobs.append((path, sha.decode("ascii"), int(mode, 8) & 0o777, length))
    stream = io.BytesIO(
        _git(
            workspace,
            "cat-file",
            "--batch",
            input_bytes=b"".join(sha.encode() + b"\n" for _, sha, _, _ in blobs),
        )
    )
    for path, sha, mode, length in blobs:
        if stream.readline().split() != [sha.encode(), b"blob", str(length).encode()]:
            raise SecurityError("Base blob header mismatch")
        data = stream.read(length)
        if len(data) != length or stream.read(1) != b"\n":
            raise SecurityError("Base blob content is incomplete")
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)


def _capture(workspace: Path) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    entries: list[dict[str, Any]] = []
    contents: dict[str, bytes] = {}
    identities: set[str] = set()
    total = 0
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def visit(fd: int, prefix: str) -> None:
        nonlocal total
        for name in sorted(os.listdir(fd)):
            if not prefix and name == ".git":
                continue
            path = _path(f"{prefix}/{name}" if prefix else name)
            identity = normalize_workspace_path(path).identity
            if identity in identities or len(entries) >= MAX_ARTIFACT_ENTRIES:
                raise SecurityError("Artifact aliases or entry limit exceeded")
            identities.add(identity)
            metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                entries.append({"path": path, "kind": "directory", "mode": 0o755})
                child = os.open(name, flags, dir_fd=fd)
                try:
                    visit(child, path)
                finally:
                    os.close(child)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                if metadata.st_mode & 0o7000:
                    raise SecurityError("Special permission bits are unsupported in artifacts")
                file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(file_fd, "rb") as source:
                    opened = os.fstat(source.fileno())
                    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                        raise SecurityError("Artifact source changed type")
                    data = source.read(MAX_ARTIFACT_BYTES - total + 1)
                    after = os.fstat(source.fileno())
                    if (opened.st_size, opened.st_mtime_ns, opened.st_mode) != (
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_mode,
                    ) or len(data) != opened.st_size:
                        raise SecurityError("Artifact source changed during capture")
                total += len(data)
                if total > MAX_ARTIFACT_BYTES:
                    raise SecurityError("Artifact byte limit exceeded")
                contents[path] = data
                entries.append(
                    {
                        "path": path,
                        "kind": "file",
                        "mode": stat.S_IMODE(opened.st_mode),
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
            else:
                raise SecurityError("Artifacts reject symlinks, hardlinks and special files")

    root_fd = os.open(workspace, flags)
    try:
        visit(root_fd, "")
    finally:
        os.close(root_fd)
    return sorted(entries, key=lambda entry: entry["path"]), contents


def _extract(archive: tarfile.TarFile, root: Path, expected: list[dict] | None = None) -> None:
    seen: set[str] = set()
    total = 0
    by_path = {item["path"]: item for item in expected} if expected is not None else None
    for member in archive:
        path = _path(member.name.rstrip("/"))
        if path in seen or len(seen) >= MAX_ARTIFACT_ENTRIES:
            raise SecurityError("Duplicate artifact path or entry limit exceeded")
        seen.add(path)
        if not member.isdir() and not member.isfile():
            raise SecurityError("Archive links and special files are unsupported")
        if member.mode & ~0o777:
            raise SecurityError("Unsupported archive permission bits")
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if member.isdir():
            entry = {"path": path, "kind": "directory", "mode": 0o755}
            target.mkdir(exist_ok=True)
        else:
            total += member.size
            if total > MAX_ARTIFACT_BYTES or member.size < 0:
                raise SecurityError("Artifact byte limit exceeded")
            source = archive.extractfile(member)
            if source is None:
                raise SecurityError("Missing artifact content")
            data = source.read(member.size + 1)
            entry = {
                "path": path,
                "kind": "file",
                "mode": member.mode,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            target.write_bytes(data)
            target.chmod(member.mode)
        if by_path is not None and entry != by_path.get(path):
            raise SecurityError("Artifact content does not match its manifest")
    if by_path is not None and seen != set(by_path):
        raise SecurityError("Artifact manifest has missing entries")


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root

    async def freeze(
        self,
        workspace: Path,
        *,
        task_id: str,
        base_sha: str,
        generation: int,
        policy_version: str,
    ) -> Candidate:
        return await asyncio.to_thread(
            self._freeze, workspace, task_id, base_sha, generation, policy_version
        )

    def _freeze(
        self,
        workspace: Path,
        task_id: str,
        base_sha: str,
        generation: int,
        policy_version: str,
    ) -> Candidate:
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base_sha):
            raise SecurityError("An actual base commit is required to freeze a candidate")
        entries, contents = _capture(workspace)
        manifest = {"schema_version": "manifest-v1", "base_sha": base_sha, "entries": entries}
        digest = digest_json(manifest)
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise SecurityError("Artifact storage cannot be a symlink")
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            staging = Path(temporary)
            bundle = staging / "bundle"
            bundle.mkdir()
            (bundle / "manifest.json").write_bytes(canonical_json(manifest))
            with tarfile.open(bundle / "tree.tar", "w") as archive:
                for entry in entries:
                    info = tarfile.TarInfo(entry["path"])
                    info.mode = entry["mode"]
                    if entry["kind"] == "directory":
                        info.type = tarfile.DIRTYPE
                        archive.addfile(info)
                    else:
                        info.size = entry["size"]
                        archive.addfile(info, io.BytesIO(contents[entry["path"]]))
            tree, base = staging / "tree", staging / "base"
            tree.mkdir()
            base.mkdir()
            with tarfile.open(bundle / "tree.tar") as archive:
                _extract(archive, tree, entries)
            _write_base(workspace, base_sha, base)
            # --no-index includes additions, deletions, modes and binary patches. The
            # stable base/tree prefixes are documented in the full-diff API.
            diff = _git(
                staging,
                "diff",
                "--no-index",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--",
                "base",
                "tree",
            )
            (bundle / "full.diff").write_bytes(diff)
            candidate = Candidate(
                candidate_id="",
                task_id=task_id,
                generation=generation,
                base_sha=base_sha,
                artifact_digest=digest,
                artifact_ref=f"sha256:{digest}",
                policy_version=policy_version,
                diff_digest=hashlib.sha256(diff).hexdigest(),
                manifest=manifest,
            )
            candidate = candidate.model_copy(update={"candidate_id": candidate_identity(candidate)})
            destination = self.root / digest
            if destination.exists():
                self.read_diff(candidate)
                with self.materialize(candidate):
                    pass
            else:
                try:
                    bundle.rename(destination)
                except OSError:
                    if not destination.exists():
                        raise
                    self.read_diff(candidate)
                    with self.materialize(candidate):
                        pass
        return candidate

    def _bundle(self, candidate: Candidate) -> Path:
        digest = candidate.artifact_digest
        if not _SHA.fullmatch(digest) or candidate.artifact_ref != f"sha256:{digest}":
            raise SecurityError("Invalid artifact reference")
        if candidate_identity(candidate) != candidate.candidate_id:
            raise SecurityError("Candidate identity mismatch")
        bundle = self.root / digest
        if self.root.is_symlink() or bundle.is_symlink():
            raise SecurityError("Artifact storage cannot contain symlinks")
        for name in ("manifest.json", "tree.tar", "full.diff"):
            if (bundle / name).is_symlink() or not (bundle / name).is_file():
                raise SecurityError("Artifact is missing or corrupt")
        manifest = json.loads((bundle / "manifest.json").read_bytes())
        if digest_json(manifest) != digest or manifest != candidate.manifest:
            raise SecurityError("Artifact manifest digest mismatch")
        if manifest.get("base_sha") != candidate.base_sha:
            raise SecurityError("Artifact base commit mismatch")
        return bundle

    def read_diff(self, candidate: Candidate) -> bytes:
        data = (self._bundle(candidate) / "full.diff").read_bytes()
        if hashlib.sha256(data).hexdigest() != candidate.diff_digest:
            raise SecurityError("Artifact diff digest mismatch")
        return data

    @contextmanager
    def materialize(self, candidate: Candidate) -> Iterator[Path]:
        bundle = self._bundle(candidate)
        with tempfile.TemporaryDirectory(prefix="repopilot-candidate-") as directory:
            root = Path(directory)
            with tarfile.open(bundle / "tree.tar") as archive:
                _extract(archive, root, candidate.manifest["entries"])
            yield root

    def validate_edits(self, candidate: Candidate, edits: list[dict]) -> None:
        with self.materialize(candidate) as root:
            for edit in edits:
                path = _path(edit["path"])
                if (root / path).read_bytes() != edit["content"].encode("utf-8"):
                    raise SecurityError("Publish intent differs from the approved artifact")


def bound_resume(state: dict, *, approved: bool, feedback: str = "") -> dict:
    """Offline test/evaluation helper; production requires a persisted decision ID."""
    return {
        "schema_version": "approval-v2",
        "candidate_id": state["candidate"]["candidate_id"],
        "evidence_digest": state["test_evidence"]["evidence_digest"],
        "approved": approved,
        "feedback": feedback,
    }
