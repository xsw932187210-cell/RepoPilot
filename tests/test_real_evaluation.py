from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from repopilot.config import Settings
from repopilot.github import GitHubPublisher
from repopilot.graph import GraphDependencies, build_graph
from repopilot.llm import MockAgentModel
from repopilot.models import CodeChangeOutput, FileEdit
from repopilot.policy_feedback import content_sha256
from repopilot.real_evaluation import OfflineWorkspace
from repopilot.repository import RepositoryContext, WorkspaceManager, run_process
from repopilot.sandbox import LocalSandbox
from repopilot.security import SecurityError


def test_offline_harness_protects_acceptance_files(tmp_path):
    manager = OfflineWorkspace(Settings(_env_file=None), None, tmp_path, tmp_path)
    manager.original = {"thefuck/rules/example.py": "old"}
    with pytest.raises(SecurityError):
        manager.apply_edits(tmp_path, [FileEdit(path="tests/test_rule.py", content="assert True")])
    with pytest.raises(SecurityError):
        manager.apply_edits(tmp_path, [FileEdit(path="thefuck/new.py", content="pass")])


def test_offline_harness_keeps_existing_source_editable(tmp_path):
    source = tmp_path / "thefuck" / "rules" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text("old", encoding="utf-8")
    manager = OfflineWorkspace(Settings(_env_file=None), None, tmp_path, tmp_path)
    manager.original = {"thefuck/rules/example.py": "old"}

    assert manager.apply_edits(
        tmp_path,
        [FileEdit(path="thefuck/rules/example.py", content="new")],
        expected_contents={"thefuck/rules/example.py": "old"},
    ) == ["thefuck/rules/example.py"]
    assert source.read_text(encoding="utf-8") == "new"


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


async def test_frozen_workflow_supports_history_free_evaluation_export(tmp_path):
    settings = Settings(
        _env_file=None,
        workspace_root=tmp_path / "workspaces",
        github_write_enabled=False,
        demo_repository_root=Path(__file__).parents[1] / "examples" / "buggy_calculator",
    )
    source_manager = WorkspaceManager(settings)
    source = await source_manager.prepare("upstream", "demo://buggy-calculator", "main")
    rule = source / "thefuck" / "rules" / "example.py"
    rule.parent.mkdir(parents=True)
    rule.write_text("def add(a, b):\n    return a - b\n")
    (source / "tests" / "test_rule.py").write_text(
        "from thefuck.rules.example import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    for args in [("git", "add", "."), ("git", "commit", "-m", "offline fixture")]:
        assert (await run_process(*args, cwd=source))[0] == 0
    sha = await source_manager.base_commit(source)
    task = SimpleNamespace(source=SimpleNamespace(buggy_commit=sha))
    manager = OfflineWorkspace(settings, task, source, tmp_path / "agent")

    class RuleModel(MockAgentModel):
        async def propose_changes(self, title, body, plan, context, feedback):
            path = "thefuck/rules/example.py"
            before = context.files[path]
            return CodeChangeOutput(
                summary="Fix add",
                edits=[
                    FileEdit(
                        path=path,
                        content=before.replace("a - b", "a + b"),
                        expected_content_sha256=content_sha256(before),
                    )
                ],
            )

    graph = build_graph(
        GraphDependencies(
            settings=settings,
            model=RuleModel(),
            workspaces=manager,
            sandbox=LocalSandbox(settings),
            publisher=GitHubPublisher(settings),
        ),
        InMemorySaver(),
    )
    result = await graph.ainvoke(
        {
            "task_id": "history-free-export",
            "repository_url": "demo://buggy-calculator",
            "base_branch": "benchmark",
            "issue_title": "Fix add returning wrong result",
            "issue_body": "The add function subtracts b instead of adding.",
            "test_command": "python -m pytest -q tests/test_rule.py",
            "max_iterations": 1,
        },
        config={"configurable": {"thread_id": "history-free-export"}},
    )
    assert result["__interrupt__"] and result["test_result"]["exit_code"] == 0
    assert result["base_sha"] == result["candidate"]["base_sha"] == sha
    assert not (manager.destination / ".git").exists()
    assert all(
        ".git" not in item["path"].split("/") for item in result["candidate"]["manifest"]["entries"]
    )
