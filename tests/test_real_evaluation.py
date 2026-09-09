from unittest.mock import patch

import pytest

from repopilot.config import Settings
from repopilot.models import FileEdit
from repopilot.real_evaluation import OfflineWorkspace
from repopilot.repository import RepositoryContext, WorkspaceManager
from repopilot.security import SecurityError


def test_offline_harness_protects_acceptance_files(tmp_path):
    manager = OfflineWorkspace(Settings(_env_file=None), None, tmp_path, tmp_path)
    manager.original = {"thefuck/rules/example.py": "old"}
    with pytest.raises(SecurityError):
        manager.apply_edits(tmp_path, [FileEdit(path="tests/test_rule.py", content="assert True")])
    with pytest.raises(SecurityError):
        manager.apply_edits(tmp_path, [FileEdit(path="thefuck/new.py", content="pass")])


def test_initial_retrieval_policy_is_shared(tmp_path):
    manager = OfflineWorkspace(Settings(_env_file=None), None, tmp_path, tmp_path)
    manager.original = {"thefuck/rules/example.py": "source"}
    context = RepositoryContext(
        tree=["tests/test_example.py", "thefuck/rules/example.py"],
        files={
            "tests/test_example.py": "test",
            "thefuck/rules/example.py": "source",
        },
    )
    with patch.object(WorkspaceManager, "inspect", return_value=context) as inspect:
        result = manager.inspect(tmp_path, "issue", ["planner", "hints"])
        inspect.assert_called_once_with(tmp_path, "issue", [])
    assert result.files == context.files
    assert result.editable_paths == ("thefuck/rules/example.py",)
