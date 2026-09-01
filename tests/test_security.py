from pathlib import Path

import pytest

from repopilot.security import (
    SecurityError,
    parse_test_command,
    safe_workspace_path,
    validate_repository_url,
)


def test_repository_url_allowlist() -> None:
    assert validate_repository_url("https://github.com/example/project") == ("example", "project")
    assert validate_repository_url("demo://buggy-calculator") == ("demo", "buggy-calculator")
    with pytest.raises(SecurityError):
        validate_repository_url("https://example.com/repository")
    with pytest.raises(SecurityError):
        validate_repository_url("https://token@github.com/example/project")


def test_path_cannot_escape_workspace(tmp_path: Path) -> None:
    assert safe_workspace_path(tmp_path, "src/app.py") == (tmp_path / "src/app.py").resolve()
    with pytest.raises(SecurityError):
        safe_workspace_path(tmp_path, "../secret")
    with pytest.raises(SecurityError):
        safe_workspace_path(tmp_path, ".git/config")


def test_test_command_has_no_shell_control() -> None:
    assert parse_test_command("python -m pytest -q") == ["python", "-m", "pytest", "-q"]
    with pytest.raises(SecurityError):
        parse_test_command("pytest ; rm -rf /tmp/example")
    with pytest.raises(SecurityError):
        parse_test_command("bash -lc pytest")
