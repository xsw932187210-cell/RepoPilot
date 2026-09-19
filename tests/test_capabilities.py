from __future__ import annotations

import os
from pathlib import Path

import pytest

from repopilot.capabilities import (
    CAPABILITY_POLICY_VERSION,
    CreateCapability,
    PathCapability,
    WorkspaceCapabilityPolicy,
    default_workspace_policy,
)
from repopilot.config import Settings
from repopilot.models import FileEdit
from repopilot.repository import WorkspaceManager
from repopilot.security import (
    CapabilityAction,
    CapabilityError,
    CapabilityReason,
    normalize_workspace_path,
)


def edit(path: str, content: str) -> FileEdit:
    return FileEdit(path=path, content=content, reason="capability test")


def test_default_policy_separates_read_write_and_unsupported_actions() -> None:
    policy = default_workspace_policy(max_file_bytes=1_000)

    assert policy.version == CAPABILITY_POLICY_VERSION
    assert policy.permits(CapabilityAction.READ, "tests/test_rule.py")
    assert not policy.permits(CapabilityAction.WRITE, "tests/test_rule.py")
    assert not policy.permits(CapabilityAction.WRITE, ".github/workflows/ci.yml")
    assert not policy.permits(CapabilityAction.READ, ".env")
    assert policy.permits(CapabilityAction.WRITE, "src/rule.py")

    target = normalize_workspace_path("src/rule.py")
    for action in (CapabilityAction.DELETE, CapabilityAction.RENAME):
        with pytest.raises(CapabilityError) as rejection:
            policy.require(action, target)
        assert rejection.value.reason is CapabilityReason.ACTION_NOT_SUPPORTED
        assert rejection.value.action is action


def test_readonly_context_edit_rejects_entire_batch(tmp_path: Path) -> None:
    source = tmp_path / "src" / "rule.py"
    test = tmp_path / "tests" / "test_rule.py"
    source.parent.mkdir()
    test.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    test.write_text("assert VALUE == 1\n", encoding="utf-8")
    manager = WorkspaceManager(Settings(_env_file=None))

    with pytest.raises(CapabilityError) as rejection:
        manager.apply_edits(
            tmp_path,
            [edit("src/rule.py", "VALUE = 2\n"), edit("tests/test_rule.py", "assert False\n")],
            expected_contents={
                "src/rule.py": "VALUE = 1\n",
                "tests/test_rule.py": "assert VALUE == 1\n",
            },
        )

    assert rejection.value.reason is CapabilityReason.WRITE_DENIED
    assert rejection.value.path == "tests/test_rule.py"
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert test.read_text(encoding="utf-8") == "assert VALUE == 1\n"


@pytest.mark.parametrize(
    "path",
    [".github/workflows/check.py", "setup.py", "src/credentials.py"],
)
def test_protected_existing_files_are_denied_at_write_boundary(
    tmp_path: Path, path: str
) -> None:
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old\n", encoding="utf-8")
    manager = WorkspaceManager(Settings(_env_file=None))

    with pytest.raises(CapabilityError) as rejection:
        manager.apply_edits(
            tmp_path,
            [edit(path, "new\n")],
            expected_contents={path: "old\n"},
        )

    assert rejection.value.reason is CapabilityReason.WRITE_DENIED
    assert target.read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize("alias", ["src/./rule.py", "SRC/RULE.py", "src\\rule.py"])
def test_duplicate_aliases_are_rejected_before_writes(tmp_path: Path, alias: str) -> None:
    source = tmp_path / "src" / "rule.py"
    source.parent.mkdir()
    source.write_text("old\n", encoding="utf-8")
    manager = WorkspaceManager(Settings(_env_file=None))

    with pytest.raises(CapabilityError) as rejection:
        manager.apply_edits(
            tmp_path,
            [edit("src/rule.py", "first\n"), edit(alias, "second\n")],
            expected_contents={"src/rule.py": "old\n"},
        )

    assert rejection.value.reason is CapabilityReason.DUPLICATE_PATH
    assert source.read_text(encoding="utf-8") == "old\n"


def test_create_requires_trusted_scope_and_expected_absent(tmp_path: Path) -> None:
    policy = WorkspaceCapabilityPolicy(
        read=PathCapability(allow=("*",)),
        write=PathCapability(allow=("*.py",)),
        create=CreateCapability(
            directories=("src/generated",),
            extensions=(".py",),
            max_bytes=20,
        ),
    )
    manager = WorkspaceManager(Settings(_env_file=None), capability_policy=policy)

    with pytest.raises(CapabilityError) as missing_precondition:
        manager.apply_edits(tmp_path, [edit("src/generated/new.py", "value = 1\n")])
    assert missing_precondition.value.reason is CapabilityReason.EXPECTED_ABSENT_REQUIRED

    with pytest.raises(CapabilityError) as wrong_directory:
        manager.apply_edits(
            tmp_path,
            [edit("src/new.py", "value = 1\n")],
            expected_absent={"src/new.py"},
        )
    assert wrong_directory.value.reason is CapabilityReason.CREATE_DENIED

    with pytest.raises(CapabilityError) as wrong_extension:
        manager.apply_edits(
            tmp_path,
            [edit("src/generated/new.txt", "value")],
            expected_absent={"src/generated/new.txt"},
        )
    assert wrong_extension.value.reason is CapabilityReason.CREATE_DENIED

    with pytest.raises(CapabilityError) as oversized:
        manager.apply_edits(
            tmp_path,
            [edit("src/generated/large.py", "x" * 21)],
            expected_absent={"src/generated/large.py"},
        )
    assert oversized.value.reason is CapabilityReason.CREATE_DENIED

    changed = manager.apply_edits(
        tmp_path,
        [edit("src/generated/new.py", "value = 1\n")],
        expected_absent={"src/generated/new.py"},
    )
    assert changed == ["src/generated/new.py"]
    assert (tmp_path / "src/generated/new.py").read_text(encoding="utf-8") == "value = 1\n"


def test_default_create_is_denied_even_with_expected_absent(tmp_path: Path) -> None:
    manager = WorkspaceManager(Settings(_env_file=None))
    with pytest.raises(CapabilityError) as rejection:
        manager.apply_edits(
            tmp_path,
            [edit("src/new.py", "value = 1\n")],
            expected_absent={"src/new.py"},
        )
    assert rejection.value.reason is CapabilityReason.CREATE_DENIED
    assert not (tmp_path / "src/new.py").exists()


def test_trusted_policy_can_explicitly_allow_test_writes(tmp_path: Path) -> None:
    test = tmp_path / "tests" / "test_rule.py"
    test.parent.mkdir()
    test.write_text("assert False\n", encoding="utf-8")
    policy = default_workspace_policy(max_file_bytes=120_000, allow_test_writes=True)
    manager = WorkspaceManager(Settings(_env_file=None), capability_policy=policy)

    assert manager.apply_edits(
        tmp_path,
        [edit("tests/test_rule.py", "assert True\n")],
        expected_contents={"tests/test_rule.py": "assert False\n"},
    ) == ["tests/test_rule.py"]


def test_links_special_files_and_credentials_are_not_capabilities(tmp_path: Path) -> None:
    source = tmp_path / "src" / "rule.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=synthetic\n", encoding="utf-8")
    (tmp_path / "linked.py").symlink_to(source)
    hardlink = tmp_path / "hardlink.py"
    os.link(source, hardlink)
    fifo = tmp_path / "pipe.py"
    if hasattr(os, "mkfifo"):
        os.mkfifo(fifo)

    manager = WorkspaceManager(Settings(_env_file=None, max_context_files=12))
    context = manager.inspect(tmp_path, "rule value linked hardlink pipe token", [])
    assert ".env" not in context.tree
    assert "linked.py" not in context.tree
    assert "hardlink.py" not in context.tree
    assert "pipe.py" not in context.tree

    with pytest.raises(CapabilityError) as linked_rejection:
        manager.apply_edits(tmp_path, [edit("linked.py", "value = 2\n")])
    assert linked_rejection.value.reason is CapabilityReason.UNSAFE_LINK

    with pytest.raises(CapabilityError) as hardlink_rejection:
        manager.apply_edits(tmp_path, [edit("hardlink.py", "value = 2\n")])
    assert hardlink_rejection.value.reason is CapabilityReason.HARDLINK

    if fifo.exists():
        with pytest.raises(CapabilityError) as special_rejection:
            manager.apply_edits(tmp_path, [edit("pipe.py", "value = 2\n")])
        assert special_rejection.value.reason is CapabilityReason.SPECIAL_FILE
