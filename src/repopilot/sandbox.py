from __future__ import annotations

import asyncio
import os
import stat
import tarfile
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

import docker
from docker.errors import ContainerError, DockerException

from repopilot.config import Settings
from repopilot.models import SandboxResult
from repopilot.repository import run_process
from repopilot.security import SecurityError, parse_test_command, redact_secrets

MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024
MAX_SNAPSHOT_ENTRIES = 10_000


def _write_workspace_snapshot(workspace: Path, destination: BinaryIO) -> None:
    """Copy only regular files/directories, without following links during traversal."""
    total_bytes = 0
    entry_count = 0
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    with tarfile.open(fileobj=destination, mode="w") as archive:

        def add_directory(directory_fd: int, archive_path: str) -> None:
            nonlocal total_bytes, entry_count
            directory = tarfile.TarInfo(archive_path)
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            directory.uid = directory.gid = 10001
            archive.addfile(directory)
            for name in sorted(os.listdir(directory_fd)):
                entry_count += 1
                if entry_count > MAX_SNAPSHOT_ENTRIES:
                    raise SecurityError("Sandbox workspace exceeds snapshot entry limit")
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                entry_path = f"{archive_path}/{name}"
                if stat.S_ISDIR(metadata.st_mode):
                    child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                    try:
                        add_directory(child_fd, entry_path)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(metadata.st_mode):
                    file_fd = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
                    )
                    with os.fdopen(file_fd, "rb") as source:
                        opened = os.fstat(source.fileno())
                        if not stat.S_ISREG(opened.st_mode):
                            raise SecurityError("Sandbox workspace contains a special file")
                        total_bytes += opened.st_size
                        if total_bytes > MAX_SNAPSHOT_BYTES:
                            raise SecurityError("Sandbox workspace exceeds snapshot byte limit")
                        entry = tarfile.TarInfo(entry_path)
                        entry.size = opened.st_size
                        entry.mode = 0o755 if opened.st_mode & 0o111 else 0o644
                        entry.uid = entry.gid = 10001
                        archive.addfile(entry, source)
                else:
                    raise SecurityError("Sandbox workspace contains a symlink or special file")

        root_fd = os.open(workspace, directory_flags)
        try:
            add_directory(root_fd, "workspace")
        finally:
            os.close(root_fd)
    destination.seek(0)


class LocalSandbox:
    """Test-only runner. Production uses DockerSandbox."""

    def __init__(self, settings: Settings):
        self.settings = settings

    async def run(self, workspace: Path, command: str) -> SandboxResult:
        args = parse_test_command(command)
        started = time.perf_counter()
        try:
            code, stdout, stderr = await run_process(
                *args,
                cwd=workspace,
                timeout_seconds=self.settings.sandbox_timeout_seconds,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            timed_out = False
        except TimeoutError:
            code, stdout, stderr, timed_out = 124, "", "Sandbox timed out", True
        return SandboxResult(
            command=args,
            exit_code=code,
            stdout=redact_secrets(stdout)[-20_000:],
            stderr=redact_secrets(stderr)[-20_000:],
            duration_ms=int((time.perf_counter() - started) * 1000),
            timed_out=timed_out,
        )


class DockerSandbox:
    """Run tests against a disposable snapshot; never mount worker/host directories."""

    def __init__(self, settings: Settings):
        self.settings = settings

    async def run(self, workspace: Path, command: str) -> SandboxResult:
        args = parse_test_command(command)
        return await asyncio.to_thread(self._run_blocking, workspace, args)

    def _run_blocking(self, workspace: Path, args: list[str]) -> SandboxResult:
        workspace = workspace.absolute()
        started = time.perf_counter()
        client = docker.from_env(timeout=self.settings.sandbox_timeout_seconds + 10)
        container = None
        kwargs: dict[str, object] = {
            "image": self.settings.sandbox_image,
            "command": args,
            "working_dir": "/workspace",
            "network_disabled": True,
            "mem_limit": self.settings.sandbox_memory,
            "pids_limit": 128,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges"],
            "user": "10001:10001",
        }
        timed_out = False
        stdout = ""
        stderr = ""
        exit_code = 1
        try:
            with tempfile.TemporaryFile() as snapshot:
                _write_workspace_snapshot(workspace, snapshot)
                container = client.containers.create(**kwargs)
                if not container.put_archive("/", snapshot):
                    raise RuntimeError("Docker sandbox could not load the workspace snapshot")
            container.start()
            try:
                result = container.wait(timeout=self.settings.sandbox_timeout_seconds)
                exit_code = int(result.get("StatusCode", 1))
            except Exception as exc:  # Docker SDK uses transport-specific timeout types.
                timed_out = True
                exit_code = 124
                stderr = f"Sandbox timed out: {type(exc).__name__}"
                container.kill()
            logs = container.logs(stdout=True, stderr=False).decode(errors="replace")
            errors = container.logs(stdout=False, stderr=True).decode(errors="replace")
            stdout = logs or stdout
            stderr = errors or stderr
        except ContainerError as exc:
            exit_code = exc.exit_status
            stderr = str(exc)
        except DockerException as exc:
            raise RuntimeError(f"Docker sandbox failed: {type(exc).__name__}: {exc}") from exc
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except DockerException:
                    pass
            client.close()
        return SandboxResult(
            command=args,
            exit_code=exit_code,
            stdout=redact_secrets(stdout)[-20_000:],
            stderr=redact_secrets(stderr)[-20_000:],
            duration_ms=int((time.perf_counter() - started) * 1000),
            timed_out=timed_out,
        )


def build_sandbox(settings: Settings) -> LocalSandbox | DockerSandbox:
    if settings.sandbox_backend == "docker":
        return DockerSandbox(settings)
    if settings.sandbox_backend == "local":
        return LocalSandbox(settings)
    raise ValueError(f"Unknown sandbox backend: {settings.sandbox_backend}")
