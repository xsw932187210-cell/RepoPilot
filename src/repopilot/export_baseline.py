"""Trusted Git objects for the exact history-free evaluation export.

No checkout, attributes/filter execution, upstream hooks or submodule fetching.
The private object repository never enters model/test workspaces or publication.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

from repopilot.candidates import capture_manifest
from repopilot.repository import run_process_bytes
from repopilot.security import SecurityError, safe_workspace_path

EXPORTED_BASELINE_VERSION = "exported-buggy-tree-v1"


async def exported_baseline(workspace: Path, repository: Path, upstream_sha: str) -> str:
    manifest = capture_manifest(workspace, upstream_sha)
    await asyncio.to_thread(repository.mkdir, mode=0o700, parents=True, exist_ok=False)
    # Git plumbing must not inherit user filters, replacement objects or hooks.
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "RepoPilot Benchmark", "GIT_COMMITTER_NAME": "RepoPilot Benchmark",
        "GIT_AUTHOR_EMAIL": "benchmark@example.invalid",
        "GIT_COMMITTER_EMAIL": "benchmark@example.invalid",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }

    async def git(*args: str, data: bytes | None = None) -> str:
        code, stdout, _ = await run_process_bytes(
            "git", *args, cwd=repository, env=environment, input_bytes=data,
        )
        if code:
            raise SecurityError("exported_baseline_git_failed")
        return stdout.decode().strip()

    await git("init", "--bare", "--template=")
    directories: dict[tuple[str, ...], list[bytes]] = {(): []}
    for entry in manifest["entries"]:
        parts = tuple(entry["path"].split("/"))
        if entry["kind"] == "directory":
            directories.setdefault(parts, [])
            continue
        descriptor = os.open(
            safe_workspace_path(workspace, entry["path"]), os.O_RDONLY | os.O_NOFOLLOW,
        )
        with os.fdopen(descriptor, "rb") as source:
            content = source.read(entry["size"] + 1)
        if len(content) != entry["size"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise SecurityError("exported_baseline_content_changed")
        blob = await git("hash-object", "-w", "--stdin", data=content)
        mode = "100755" if entry["mode"] & 0o111 else "100644"
        directories.setdefault(parts[:-1], []).append(
            f"{mode} blob {blob}\t{parts[-1]}".encode() + b"\0"
        )
    for parts in sorted(directories, key=lambda value: (-len(value), value)):
        tree = await git("mktree", "-z", data=b"".join(sorted(directories[parts])))
        if parts:
            directories.setdefault(parts[:-1], []).append(
                f"040000 tree {tree}\t{parts[-1]}".encode() + b"\0"
            )
        else:
            root_tree = tree
    commit = await git("commit-tree", root_tree, data=(
        f"{EXPORTED_BASELINE_VERSION}\nupstream buggy revision: {upstream_sha}\n"
    ).encode())
    if capture_manifest(workspace, upstream_sha) != manifest:
        raise SecurityError("exported_baseline_changed_during_capture")
    return commit
