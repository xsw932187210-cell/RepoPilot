from pathlib import Path

import pytest

from repopilot.security import (
    SecurityError,
    parse_test_command,
    safe_workspace_path,
    validate_branch,
    validate_repository_url,
)


def test_repository_url_allowlist() -> None:
    assert validate_repository_url("https://github.com/example/project") == ("example", "project")
    assert validate_repository_url("demo://buggy-calculator") == ("demo", "buggy-calculator")
    assert validate_repository_url("demo://benchmark-suite") == ("demo", "benchmark-suite")
    with pytest.raises(SecurityError):
        validate_repository_url("https://example.com/repository")
    with pytest.raises(SecurityError):
        validate_repository_url("https://token@github.com/example/project")
    with pytest.raises(SecurityError):
        validate_repository_url("https://github.com/example/project?token=secret")
    with pytest.raises(SecurityError):
        validate_repository_url("demo://unknown")


def test_path_cannot_escape_workspace(tmp_path: Path) -> None:
    assert safe_workspace_path(tmp_path, "src/app.py") == (tmp_path / "src/app.py").resolve()
    with pytest.raises(SecurityError):
        safe_workspace_path(tmp_path, "../secret")
    with pytest.raises(SecurityError):
        safe_workspace_path(tmp_path, ".git/config")

    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SecurityError):
        safe_workspace_path(tmp_path, "escape/secret.txt")


def test_test_command_has_no_shell_control() -> None:
    assert parse_test_command("python -m pytest -q") == ["python", "-m", "pytest", "-q"]
    with pytest.raises(SecurityError):
        parse_test_command("pytest ; rm -rf /tmp/example")
    with pytest.raises(SecurityError):
        parse_test_command("bash -lc pytest")
    with pytest.raises(SecurityError):
        parse_test_command("python -c 'print(1)'")
    with pytest.raises(SecurityError):
        parse_test_command("ruff format .")


def test_branch_validation_rejects_ambiguous_refs() -> None:
    assert validate_branch("feature/safe-change") == "feature/safe-change"
    with pytest.raises(SecurityError):
        validate_branch("feature/../main")
    with pytest.raises(SecurityError):
        validate_branch("main/")
