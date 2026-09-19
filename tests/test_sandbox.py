from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path
from typing import BinaryIO

import pytest

from repopilot.config import Settings
from repopilot.sandbox import DockerSandbox, _write_workspace_snapshot
from repopilot.security import SecurityError


class SnapshotContainer:
    def __init__(self, *, upload_succeeds: bool = True, timeout: bool = False) -> None:
        self.upload_succeeds = upload_succeeds
        self.timeout = timeout
        self.archive = b""
        self.started = self.removed = self.killed = False

    def put_archive(self, path: str, data: BinaryIO) -> bool:
        assert path == "/"
        self.archive = data.read()
        return self.upload_succeeds

    def start(self) -> None:
        assert self.archive
        self.started = True

    def wait(self, timeout: int) -> dict[str, int]:
        assert timeout > 0
        if self.timeout:
            raise TimeoutError
        return {"StatusCode": 0}

    def logs(self, *, stdout: bool, stderr: bool) -> bytes:
        return b"1 passed" if stdout and not stderr else b""

    def kill(self) -> None:
        self.killed = True

    def remove(self, *, force: bool) -> None:
        self.removed = force


class SnapshotClient:
    def __init__(self, container: SnapshotContainer) -> None:
        self.container = container
        self.containers = self
        self.kwargs: dict[str, object] = {}
        self.closed = False

    def create(self, **kwargs: object) -> SnapshotContainer:
        self.kwargs = kwargs
        return self.container

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("running_in_container", [False, True])
async def test_docker_uses_only_current_task_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, running_in_container: bool
) -> None:
    workspace = tmp_path / "task-a"
    workspace.mkdir()
    (workspace / "test_example.py").write_text("def test_ok(): assert True\n")
    sibling = tmp_path / "task-b"
    sibling.mkdir()
    (sibling / "private.txt").write_text("another task")
    container = SnapshotContainer()
    client = SnapshotClient(container)
    monkeypatch.setattr("repopilot.sandbox.docker.from_env", lambda **_: client)
    monkeypatch.setenv("HOSTNAME", "worker-with-docker-socket")
    settings = Settings(_env_file=None, running_in_container=running_in_container)

    result = await DockerSandbox(settings).run(workspace, "python -m pytest -q")

    assert result.passed
    assert not {"volumes", "volumes_from", "mounts"}.intersection(client.kwargs)
    assert client.kwargs["working_dir"] == "/workspace"
    assert client.kwargs["user"] == "10001:10001"
    assert client.kwargs["network_disabled"] is True
    assert client.kwargs["mem_limit"] == settings.sandbox_memory
    assert client.kwargs["cap_drop"] == ["ALL"]
    assert client.kwargs["security_opt"] == ["no-new-privileges"]
    with tarfile.open(fileobj=io.BytesIO(container.archive)) as archive:
        assert archive.getnames() == ["workspace", "workspace/test_example.py"]
        assert all(entry.uid == 10001 and entry.gid == 10001 for entry in archive)
        entry = archive.extractfile("workspace/test_example.py")
        assert entry is not None
        assert entry.read() == (workspace / "test_example.py").read_bytes()
    assert container.started and container.removed and client.closed


@pytest.mark.parametrize("target_is_directory", [False, True])
def test_snapshot_rejects_symlinks(tmp_path: Path, target_is_directory: bool) -> None:
    workspace = tmp_path / "task"
    workspace.mkdir()
    outside = tmp_path / "outside"
    if target_is_directory:
        outside.mkdir()
    else:
        outside.write_text("private")
    (workspace / "escape").symlink_to(outside, target_is_directory=target_is_directory)
    with pytest.raises(SecurityError, match="symlink"):
        _write_workspace_snapshot(workspace, io.BytesIO())


def test_snapshot_rejects_symlink_root(tmp_path: Path) -> None:
    workspace = tmp_path / "task"
    workspace.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(OSError):
        _write_workspace_snapshot(alias, io.BytesIO())


def test_snapshot_rejects_special_files(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(SecurityError, match="special file"):
        _write_workspace_snapshot(tmp_path, io.BytesIO())


@pytest.mark.parametrize("limit", ["MAX_SNAPSHOT_BYTES", "MAX_SNAPSHOT_ENTRIES"])
def test_snapshot_has_bounded_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    (tmp_path / "file.txt").write_text("too large")
    monkeypatch.setattr(f"repopilot.sandbox.{limit}", 0)
    with pytest.raises(SecurityError, match="limit"):
        _write_workspace_snapshot(tmp_path, io.BytesIO())


@pytest.mark.asyncio
async def test_upload_failure_cleans_up_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = SnapshotContainer(upload_succeeds=False)
    client = SnapshotClient(container)
    monkeypatch.setattr("repopilot.sandbox.docker.from_env", lambda **_: client)
    with pytest.raises(RuntimeError, match="could not load"):
        await DockerSandbox(Settings(_env_file=None)).run(tmp_path, "python -m pytest -q")
    assert not container.started
    assert container.removed and client.closed


@pytest.mark.asyncio
async def test_timeout_kills_and_removes_snapshot_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = SnapshotContainer(timeout=True)
    client = SnapshotClient(container)
    monkeypatch.setattr("repopilot.sandbox.docker.from_env", lambda **_: client)
    result = await DockerSandbox(Settings(_env_file=None)).run(tmp_path, "python -m pytest -q")
    assert result.exit_code == 124 and result.timed_out
    assert container.killed and container.removed and client.closed
