import runpy
from pathlib import Path

import pytest

from repopilot.chatgpt_auth import PrivateStore
from repopilot.config import Settings
from repopilot.models import CodeChangeOutput, PlanOutput, ReviewOutput


@pytest.mark.asyncio
@pytest.mark.parametrize("max_calls", [2, 4])
async def test_connection_checks_persist_reservations_and_never_reset_on_resume(
    tmp_path, monkeypatch, max_calls,
):
    script = runpy.run_path(str(Path(__file__).parents[1] / "scripts/check_model_connections.py"))
    run_checks = script["run_checks"]
    store = PrivateStore(tmp_path / "private")
    transports = []

    def factory(_settings, *, budget):
        class Fixture:
            async def invoke(self, result):
                async def transport():
                    saved = store.read("checks.json")
                    assert saved["calls"]["pending_started_calls"] == 1
                    transports.append(type(result).__name__)
                    return result

                return await budget.call(transport)

            async def plan(self, *_args):
                return await self.invoke(PlanOutput(summary="fixture", steps=["edit"]))

            async def propose_changes(self, *_args):
                return await self.invoke(CodeChangeOutput(summary="fixture", edits=[]))

            async def review(self, *_args):
                return await self.invoke(ReviewOutput(approved=False, summary="No tests run"))

        return Fixture()

    monkeypatch.setitem(run_checks.__globals__, "build_agent_model", factory)
    settings = Settings(
        _env_file=None, model_provider="google_cloud", model_name="fixture-gemini",
        review_model_provider="chatgpt", review_model_name="fixture-gpt",
    )
    first = await run_checks(settings, store, max_calls)
    assert len(transports) == first["calls"]["reserved_calls"] == max_calls
    second = await run_checks(settings, store, max_calls)
    assert len(transports) == second["calls"]["reserved_calls"] == max_calls
    assert all(value["status"] == "succeeded" for value in second["checks"].values()) == (
        max_calls == 4
    )
    with pytest.raises(ValueError, match="configuration differs"):
        await run_checks(settings, store, max_calls + 1)
    assert len(transports) == max_calls
