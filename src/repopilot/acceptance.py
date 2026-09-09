"""Isolated post-run acceptance with bounded, machine-readable JUnit evidence."""

from __future__ import annotations

import asyncio
import io
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import docker
from docker.errors import DockerException, NotFound

from repopilot.config import Settings
from repopilot.sandbox import _write_workspace_snapshot
from repopilot.security import parse_test_command, redact_secrets

MAX_REPORT_BYTES = 2 * 1024 * 1024
ACCEPTANCE_PROTOCOL_VERSION = 3


def parse_junit(data: bytes) -> dict[str, str]:
    if len(data) > MAX_REPORT_BYTES or b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError("Unsafe or oversized JUnit document")
    root = ET.fromstring(data)  # noqa: S314 -- bounded, DTD/entity-free document
    outcomes: dict[str, str] = {}
    for case in root.iter("testcase"):
        key = f"{case.get('classname', '')}::{case.get('name', '')}"
        if key in outcomes:
            raise ValueError("Duplicate JUnit test identity")
        status = "passed"
        for tag in ("skipped", "failure", "error"):
            if case.find(tag) is not None:
                status = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
        outcomes[key] = status
    if not outcomes:
        raise ValueError("Acceptance collected no tests")
    return outcomes


def compare_acceptance(
    buggy: dict[str, str], fixed: dict[str, str], candidate: dict[str, str]
) -> dict[str, object]:
    """Only buggy-failing/fixed-passing tests demonstrate a reproduced defect."""
    target = {
        key for key, value in buggy.items() if value == "failed" and fixed.get(key) == "passed"
    }
    regression_set = {
        key for key, value in buggy.items() if value == "passed" and fixed.get(key) == "passed"
    }
    regressions = sorted(key for key in regression_set if candidate.get(key) != "passed")
    missing = sorted(set(fixed) - set(candidate))
    baseline_drift = sorted(set(buggy) ^ set(fixed))
    extra = sorted(set(candidate) - set(fixed))
    return {
        "fail_to_pass_count": len(target),
        "fail_to_pass_resolved": sum(candidate.get(key) == "passed" for key in target),
        "pass_to_pass_count": len(regression_set),
        "regressions": regressions,
        "missing_tests": missing,
        "baseline_identity_drift": baseline_drift,
        "extra_tests": extra,
        "resolved": bool(target)
        and not missing
        and not baseline_drift
        and not extra
        and not regressions
        and all(candidate.get(key) == "passed" for key in target)
        and all(value == "passed" for value in candidate.values()),
    }


class AcceptanceRunner:
    """No host mounts, credentials, network or model-visible hidden-test feedback."""

    def __init__(self, settings: Settings):
        self.settings = settings

    async def run(self, workspace: Path, command: str) -> dict[str, object]:
        args = parse_test_command(command)
        if args[:3] != ["python", "-m", "pytest"]:
            raise ValueError("Real acceptance requires python -m pytest")
        if any(arg.startswith("--junit") for arg in args):
            raise ValueError("JUnit destination is evaluator-controlled")
        return await asyncio.to_thread(self._run, workspace, args)

    def _run(self, workspace: Path, args: list[str]) -> dict[str, object]:
        started = time.perf_counter()
        client = docker.from_env(timeout=self.settings.sandbox_timeout_seconds + 10)
        container = None
        try:
            container = client.containers.create(
                image=self.settings.sandbox_image,
                command=[*args, "-p", "no:cacheprovider", "--junitxml=/tmp/acceptance.xml"],
                working_dir="/workspace",
                network_disabled=True,
                mem_limit=self.settings.sandbox_memory,
                pids_limit=128,
                init=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                user="10001:10001",
                environment={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": "/workspace"},
            )
            with tempfile.TemporaryFile() as snapshot:
                _write_workspace_snapshot(workspace, snapshot)
                if not container.put_archive("/", snapshot):
                    raise RuntimeError("Acceptance snapshot transfer failed")
            container.start()
            try:
                status = container.wait(timeout=self.settings.sandbox_timeout_seconds)
            except Exception:
                try:
                    container.kill()
                except DockerException:
                    pass
                raise
            logs = container.logs().decode(errors="replace")
            # Fixed path is inside a unique disposable container, never on the host.
            try:
                chunks, _ = container.get_archive("/tmp/acceptance.xml")  # noqa: S108
            except NotFound:
                # Candidate code can prevent pytest from reaching JUnit serialization.
                # That is an unsuccessful candidate outcome, not an evaluator crash.
                return {
                    "exit_code": int(status["StatusCode"]),
                    "outcomes": {},
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "logs": redact_secrets(logs)[-20_000:],
                    "report_missing": True,
                }
            raw = bytearray()
            for chunk in chunks:
                raw.extend(chunk)
                if len(raw) > MAX_REPORT_BYTES + 20_000:
                    raise ValueError("Oversized acceptance archive")
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                members = archive.getmembers()
                if (
                    len(members) != 1
                    or not members[0].isfile()
                    or members[0].name != "acceptance.xml"
                ):
                    raise ValueError("Invalid acceptance archive")
                stream = archive.extractfile(members[0])
                if stream is None:
                    raise ValueError("Missing acceptance report")
                outcomes = parse_junit(stream.read(MAX_REPORT_BYTES + 1))
            return {
                "exit_code": int(status["StatusCode"]),
                "outcomes": outcomes,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "logs": redact_secrets(logs)[-20_000:],
            }
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except DockerException:
                    pass
            client.close()
