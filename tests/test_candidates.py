from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from repopilot.candidates import ArtifactStore
from repopilot.config import Settings
from repopilot.repository import WorkspaceManager, run_process
from repopilot.security import SecurityError


@pytest.fixture
async def repository(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        workspace_root=tmp_path / "workspaces",
        demo_repository_root=Path(__file__).parents[1] / "examples" / "buggy_calculator",
    )
    manager = WorkspaceManager(settings)
    workspace = await manager.prepare("candidate", "demo://buggy-calculator", "main")
    return workspace, await manager.base_commit(workspace), ArtifactStore(tmp_path / "artifacts")


async def freeze(repository):
    workspace, base, store = repository
    return await store.freeze(
        workspace,
        task_id="candidate",
        base_sha=base,
        generation=1,
        policy_version="test-policy",
    )


async def test_same_snapshot_is_stable_and_test_copy_cannot_change_artifact(repository):
    workspace, base, store = repository
    first = await freeze(repository)
    os.utime(workspace / "calculator.py", (10, 10))
    second = await freeze(repository)
    assert first == second
    assert first.base_sha == base
    assert all(not item["path"].startswith(".git/") for item in first.manifest["entries"])
    original = (workspace / "calculator.py").read_bytes()
    with store.materialize(first) as copy:
        (copy / "calculator.py").write_bytes(b"test mutated its disposable copy")
    (workspace / "calculator.py").write_bytes(b"workspace mutated while waiting for approval")
    with store.materialize(first) as copy:
        assert (copy / "calculator.py").read_bytes() == original


@pytest.mark.parametrize("change", ["byte", "mode", "add", "delete", "binary"])
async def test_every_file_change_generates_a_different_candidate(repository, change):
    workspace, _, store = repository
    first = await freeze(repository)
    source = workspace / "calculator.py"
    if change == "byte":
        source.write_bytes(source.read_bytes() + b" ")
    elif change == "mode":
        source.chmod(0o600 if source.stat().st_mode & 0o777 != 0o600 else 0o644)
    elif change == "add":
        (workspace / "new.py").write_text("# added\n")
    elif change == "delete":
        source.unlink()
    else:
        (workspace / "binary.bin").write_bytes(b"\x00\xff\x01")
    second = await freeze(repository)
    assert first.artifact_digest != second.artifact_digest
    assert first.candidate_id != second.candidate_id
    diff = store.read_diff(second)
    if change == "add":
        assert b"new.py" in diff
    if change == "binary":
        assert b"GIT binary patch" in diff


async def test_complete_diff_includes_changes_beyond_preview_limit(repository):
    workspace, _, store = repository
    (workspace / "large.txt").write_text("A" * 110_000 + "Z")
    first = await freeze(repository)
    (workspace / "large.txt").write_text("A" * 110_000 + "Y")
    second = await freeze(repository)
    assert first.artifact_digest != second.artifact_digest
    diff = store.read_diff(second)
    assert len(diff) > 100_000 and b"Y" in diff[100_000:]
    assert hashlib.sha256(diff).hexdigest() == second.diff_digest


@pytest.mark.parametrize("kind", ["symlink", "directory-symlink", "hardlink", "fifo"])
async def test_unsafe_entries_are_rejected(repository, tmp_path, kind):
    workspace, _, _ = repository
    if kind == "symlink":
        (workspace / "escape").symlink_to(tmp_path / "secret")
    elif kind == "directory-symlink":
        (workspace / "escape").symlink_to(tmp_path, target_is_directory=True)
    elif kind == "hardlink":
        os.link(workspace / "calculator.py", workspace / "linked.py")
    else:
        os.mkfifo(workspace / "fifo")
    with pytest.raises(SecurityError):
        await freeze(repository)


@pytest.mark.parametrize("part", ["manifest.json", "full.diff", "tree.tar"])
async def test_corrupt_artifact_is_rejected(repository, part):
    _, _, store = repository
    candidate = await freeze(repository)
    target = store.root / candidate.artifact_digest / part
    if part == "tree.tar":
        data = target.read_bytes()
        offset = data.index(b"return a - b")
        target.write_bytes(
            data[:offset] + data[offset:].replace(b"return a - b", b"return a + b", 1)
        )
        with pytest.raises(SecurityError):
            with store.materialize(candidate):
                pass
    else:
        target.write_bytes(b"{}" if part == "manifest.json" else b"corrupt")
        with pytest.raises(SecurityError):
            store.read_diff(candidate)


async def test_manifest_mode_is_preserved_in_test_copy(repository):
    workspace, _, store = repository
    (workspace / "calculator.py").chmod(0o750)
    candidate = await freeze(repository)
    with store.materialize(candidate) as copy:
        assert (copy / "calculator.py").stat().st_mode & 0o777 == 0o750


async def test_full_diff_uses_actual_base_blobs_despite_export_attributes(repository):
    workspace, _, store = repository
    (workspace / ".gitattributes").write_text("calculator.py export-ignore\n")
    for args in [("git", "add", "."), ("git", "commit", "-m", "export attribute fixture")]:
        code, _, _ = await run_process(*args, cwd=workspace)
        assert code == 0
    _, base, _ = await run_process("git", "rev-parse", "HEAD", cwd=workspace)
    candidate = await store.freeze(
        workspace,
        task_id="candidate",
        base_sha=base.strip(),
        generation=1,
        policy_version="fixture",
    )
    assert store.read_diff(candidate) == b""
