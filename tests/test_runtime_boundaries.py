from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from repopilot.config import Settings
from repopilot.models import FileEdit
from repopilot.repository import WorkspaceManager, run_process
from repopilot.security import SecurityError


def edit(path: str, content: str) -> FileEdit:
    return FileEdit(path=path, content=content, reason="test proposal")


def test_stale_context_aborts_complete_proposal(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("original a")
    (tmp_path / "b.py").write_text("other writer changed b")
    manager = WorkspaceManager(Settings(_env_file=None))
    with pytest.raises(SecurityError, match="changed since"):
        manager.apply_edits(
            tmp_path,
            [edit("a.py", "new a"), edit("b.py", "new b")],
            expected_contents={"a.py": "original a", "b.py": "original b"},
        )
    assert (tmp_path / "a.py").read_text() == "original a"
    assert (tmp_path / "b.py").read_text() == "other writer changed b"


@pytest.mark.parametrize("path", ["../outside.py", ".git/config", "unseen.py"])
def test_invalid_second_edit_does_not_partially_apply(tmp_path: Path, path: str) -> None:
    (tmp_path / "a.py").write_text("old")
    with pytest.raises(SecurityError):
        WorkspaceManager(Settings(_env_file=None)).apply_edits(
            tmp_path,
            [edit("a.py", "new"), edit(path, "bad")],
            expected_contents={"a.py": "old"},
        )
    assert (tmp_path / "a.py").read_text() == "old"


def test_duplicate_edits_rejected_and_same_context_succeeds(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("old")
    manager = WorkspaceManager(Settings(_env_file=None))
    with pytest.raises(SecurityError, match="Duplicate"):
        manager.apply_edits(tmp_path, [edit("a.py", "one"), edit("a.py", "two")])
    assert (tmp_path / "a.py").read_text() == "old"
    assert manager.apply_edits(
        tmp_path, [edit("a.py", "new")], expected_contents={"a.py": "old"}
    ) == ["a.py"]


@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_process_timeout_and_cancellation_kill_group(
    monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    process = AsyncMock()
    process.pid = 43210
    process.communicate.side_effect = failure()
    launch = AsyncMock(return_value=process)
    killed = []
    monkeypatch.setattr("repopilot.repository.asyncio.create_subprocess_exec", launch)
    monkeypatch.setattr(
        "repopilot.repository.os.killpg", lambda pid, sig: killed.append((pid, sig))
    )
    with pytest.raises(failure):
        await run_process("python", "-m", "pytest")
    assert launch.call_args.kwargs["start_new_session"] is True
    assert killed == [(43210, signal.SIGKILL)]
    process.wait.assert_awaited_once()
