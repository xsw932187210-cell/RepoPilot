#!/usr/bin/env python3
"""Prepare the owned, deterministic CH-07A fixture cache without executing tests.

This source is synthetic and must never be counted as real repair-quality evidence.
It creates only a new or empty caller-selected destination; it never clones a remote.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent


def prepare(destination: Path) -> tuple[str, str]:
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError("Synthetic cache destination must be absent or empty")
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE / "buggy", destination, dirs_exist_ok=True)
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "RepoPilot Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "RepoPilot Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
    }

    def git(*args: str) -> str:
        return subprocess.run(  # noqa: S603 -- trusted argv and new owned fixture directory
            ["git", "-C", str(destination), *args],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        ).stdout.strip()

    git("init", "-q", "--object-format=sha1", "--initial-branch=fixture")
    git("config", "core.autocrlf", "false")
    git("config", "commit.gpgsign", "false")
    git("add", ".")
    git("commit", "-qm", "Synthetic buggy fixture")
    buggy = git("rev-parse", "HEAD")
    shutil.copytree(FIXTURE / "fixed", destination, dirs_exist_ok=True)
    git("add", ".")
    git("commit", "-qm", "Synthetic fixed fixture")
    return buggy, git("rev-parse", "HEAD")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    buggy, fixed = prepare(parser.parse_args().destination)
    print(f"synthetic buggy={buggy} fixed={fixed}")


if __name__ == "__main__":
    main()
