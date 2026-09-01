from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import docker
from docker.errors import ContainerError, DockerException

from repopilot.config import Settings
from repopilot.models import SandboxResult
from repopilot.repository import run_process
from repopilot.security import parse_test_command, redact_secrets


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
    def __init__(self, settings: Settings):
        self.settings = settings

    async def run(self, workspace: Path, command: str) -> SandboxResult:
        args = parse_test_command(command)
        resolved_workspace = await asyncio.to_thread(workspace.resolve)
        return await asyncio.to_thread(self._run_blocking, resolved_workspace, args)

    def _run_blocking(self, workspace: Path, args: list[str]) -> SandboxResult:
        started = time.perf_counter()
        client = docker.from_env(timeout=self.settings.sandbox_timeout_seconds + 10)
        container = None
        kwargs: dict[str, object] = {
            "image": self.settings.sandbox_image,
            "command": args,
            "detach": True,
            "network_disabled": True,
            "mem_limit": self.settings.sandbox_memory,
            "pids_limit": 128,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges"],
            "user": "10001:10001",
        }
        if self.settings.running_in_container:
            container_id = os.environ.get("HOSTNAME", "")
            if not container_id:
                raise RuntimeError(
                    "Cannot identify the worker container for sandbox volume sharing"
                )
            kwargs["volumes_from"] = [container_id]
            kwargs["working_dir"] = str(workspace)
        else:
            kwargs["volumes"] = {str(workspace): {"bind": "/workspace", "mode": "rw"}}
            kwargs["working_dir"] = "/workspace"

        timed_out = False
        stdout = ""
        stderr = ""
        exit_code = 1
        try:
            container = client.containers.run(**kwargs)
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
