import json
from pathlib import Path

import pytest

from repopilot.chatgpt_auth import PrivateStore
from repopilot.config import Settings
from repopilot.eval_protocol import AuthorizedExperiment, prepare_plan
from repopilot.eval_repeats import RepeatConfig, RepeatedEvaluationRuntime
from repopilot.eval_runtime import EvaluationConfig


def grant(store):
    store.write("ch01c.json", {
        "task": "CH-01C", "approved_at": "2026-10-23", "max_api_attempts": 108,
        "per_case_strategy_repeat_max_attempts": 6, "cases": 3, "repeats": 2,
        "includes_failures_and_retries": True, "strategies": ["oneshot", "workflow", "auto"],
        "providers": ["google_cloud", "chatgpt"], "status": "authorized_not_started",
    })


def ledger(*ids):
    return {"fingerprint": "same-cohort", "max_api_attempts": 108,
            "reservations": [{"attempt_id": value} for value in ids]}


@pytest.mark.asyncio
async def test_authorization_binds_one_plan_and_refuses_budget_reset(tmp_path):
    store = PrivateStore(tmp_path / "private")
    grant(store)
    plan = {"source_frozen": True, "fingerprint": "plan-one"}
    output = tmp_path / "report"
    approved = AuthorizedExperiment(store, plan, output)
    await approved.observe(ledger("first"))
    assert store.read("ch01c.json")["reserved_api_attempts"] == 1
    with pytest.raises(ValueError, match="different_experiment"):
        AuthorizedExperiment(store, plan, tmp_path / "fresh-report")
    with pytest.raises(ValueError, match="different_experiment"):
        AuthorizedExperiment(store, {**plan, "fingerprint": "changed"}, output)
    with pytest.raises(ValueError, match="ledger_missing"):
        AuthorizedExperiment(store, plan, output)
    path = output / "model-records/api-reservations.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(ledger("first", "second")))
    resumed = AuthorizedExperiment(store, plan, output)
    assert resumed.record["reservation_ids"] == ["first", "second"]
    path.write_text(json.dumps(ledger("first")))
    with pytest.raises(ValueError, match="history_lost"):
        AuthorizedExperiment(store, plan, output)


def test_authorization_requires_existing_grant_and_frozen_source(tmp_path):
    store = PrivateStore(tmp_path / "private")
    with pytest.raises(ValueError, match="authorization_required"):
        AuthorizedExperiment(store, {"source_frozen": True}, tmp_path / "report")
    grant(store)
    with pytest.raises(ValueError, match="not_frozen"):
        AuthorizedExperiment(store, {"source_frozen": False}, tmp_path / "report")
    record = store.read("ch01c.json")
    record["status"] = "revoked"
    store.write("ch01c.json", record)
    with pytest.raises(ValueError, match="authorization_required"):
        AuthorizedExperiment(store, {"source_frozen": True}, tmp_path / "report")


@pytest.mark.asyncio
async def test_observer_failure_consumes_reservation_without_transport(tmp_path):
    calls = 0

    async def unavailable(budget):
        assert len(budget["reservations"]) == 1
        raise RuntimeError("private storage unavailable")

    async def transport():
        nonlocal calls
        calls += 1
        return {}

    async def execute(case, mode, budget):
        await budget.call(transport)
        return {"success": True}

    base = EvaluationConfig(dataset_sha256="a" * 64, provider="mock", model="fixture",
                            temperature=0, max_model_calls=6, modes=("oneshot",))
    runtime = RepeatedEvaluationRuntime(
        RepeatConfig(base, "observer-test", 1, "v1", 108), tmp_path / "records",
        reservation_observer=unavailable,
    )
    result = await runtime.run([{"id": "case-one"}], execute)
    assert calls == 0
    assert result["metadata"]["budget"]["reserved_api_attempts"] == 1
    assert result["metadata"]["budget"]["orphan_reservations"] == 1


def test_protocol_plan_is_read_only_and_rejects_wrong_models_or_design():
    root = Path(__file__).parents[1]
    protocol = json.loads((root / "evals/real/experiments/ch01c-pilot-v1.json").read_text())
    settings = Settings(
        _env_file=None, model_provider="google_cloud", model_name="gemini-3.5-flash",
        review_model_provider="chatgpt", review_model_name="gpt-6.1-sol",
        model_temperature=1, model_max_output_tokens=32768, model_max_calls=6,
    )
    plan = prepare_plan(protocol, settings, root)
    assert plan["model_roles"]["reviewer"]["server_max_output_tokens"] is None
    assert plan["protocol"]["max_api_attempts"] == 108
    with pytest.raises(ValueError, match="outside_approved_design"):
        prepare_plan({**protocol, "repeats": 3}, settings, root)
    with pytest.raises(ValueError, match="configured_model"):
        prepare_plan(protocol, settings.model_copy(update={"review_model_name": "another"}), root)
