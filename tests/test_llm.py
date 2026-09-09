from unittest.mock import AsyncMock

import pytest

from repopilot.config import Settings
from repopilot.llm import OpenAICompatibleAgentModel
from repopilot.models import CodeChangeOutput, PlanOutput
from repopilot.repository import RepositoryContext


@pytest.mark.asyncio
async def test_readable_context_and_edit_capabilities_are_distinct() -> None:
    model = OpenAICompatibleAgentModel(
        Settings(
            _env_file=None,
            model_provider="openai",
            model_name="test-model",
            openai_api_key="test-key",
        )
    )
    invoke = AsyncMock(return_value=CodeChangeOutput(summary="no change", edits=[]))
    model._structured_invoke = invoke
    context = RepositoryContext(
        tree=["src/rule.py", "tests/test_rule.py"],
        files={"src/rule.py": "VALUE = 1\n", "tests/test_rule.py": "assert True\n"},
        editable_paths=("src/rule.py",),
    )

    await model.propose_changes(
        "Fix rule",
        "The rule is incorrect.",
        PlanOutput(summary="fix", steps=["edit source"]),
        context,
        [],
    )

    messages = invoke.await_args.args[1]
    user_content = messages[1]["content"]
    capability_line = next(
        line for line in user_content.splitlines() if line.startswith("Allowed edit paths")
    )
    assert capability_line.endswith('["src/rule.py"]')
    assert "tests/test_rule.py" not in capability_line
    assert "--- tests/test_rule.py ---" in user_content
