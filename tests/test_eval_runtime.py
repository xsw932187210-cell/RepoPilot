from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from repopilot.config import Settings
from repopilot.eval_runtime import (
    CallBudgetExceeded,
    EvaluationConfig,
    EvaluationRuntime,
    ModelCallBudget,
    ResumeMismatchError,
    dataset_sha256,
)
from repopilot.llm import build_agent_model


def config(**overrides: Any) -> EvaluationConfig:
    values: dict[str, Any] = {
        "dataset_sha256": dataset_sha256('{"id":"case-a"}\n'),
        "provider": "mock",
        "model": "deterministic-mock-v1",
        "temperature": 0.0,
        "max_model_calls": 3,
        "modes": ("oneshot", "workflow"),
        "context_budget": {"max_files": 8, "max_chars": 40_000},
        "test_evaluator": "hidden-pytest-v1",
        "evaluator_config": {"sandbox": "docker", "timeout_seconds": 120},
    }
    values.update(overrides)
    return EvaluationConfig(**values)


@pytest.mark.asyncio
async def test_crash_checkpoint_resumes_only_unfinished_pairs(tmp_path: Path) -> None:
    cases = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    runtime = EvaluationRuntime(config(modes=("workflow",)), tmp_path / "records")
    first_calls: list[str] = []

    class SimulatedProcessCrash(BaseException):
        pass

    async def crashing(case: dict[str, Any], mode: str, budget: ModelCallBudget) -> dict:
        del mode, budget
        first_calls.append(case["id"])
        if case["id"] == "b":
            raise SimulatedProcessCrash
        return {"success": True}

    with pytest.raises(SimulatedProcessCrash):
        await runtime.run(cases, crashing)
    assert first_calls == ["a", "b"]
    checkpoint_statuses = sorted(
        json.loads(path.read_text(encoding="utf-8"))["status"]
        for path in (tmp_path / "records" / "cases").glob("*.json")
    )
    assert checkpoint_statuses == ["completed", "running"]

    resumed_calls: list[str] = []

    async def resumed(case: dict[str, Any], mode: str, budget: ModelCallBudget) -> dict:
        del mode, budget
        resumed_calls.append(case["id"])
        return {"success": True}

    report = await runtime.run(cases, resumed)
    assert resumed_calls == ["b", "c"]
    assert report["summary"]["completed"] == 3
    assert report["summary"]["failed"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"dataset_sha256": "0" * 64},
        {"provider": "different-provider"},
        {"model": "another-model"},
        {"temperature": 0.2},
        {"max_model_calls": 4},
        {"modes": ("workflow",)},
        {"context_budget": {"max_files": 9, "max_chars": 40_000}},
        {"test_evaluator": "different-tests"},
        {"evaluator_config": {"sandbox": "docker", "timeout_seconds": 121}},
    ],
)
async def test_resume_rejects_every_comparability_change(
    tmp_path: Path, changed: dict[str, Any]
) -> None:
    original = config()
    records = tmp_path / "records"

    async def execute(case: dict, mode: str, budget: ModelCallBudget) -> dict:
        del case, mode, budget
        return {"success": True}

    await EvaluationRuntime(original, records).run([{"id": "a"}], execute)
    with pytest.raises(ResumeMismatchError, match="fingerprint/config"):
        await EvaluationRuntime(replace(original, **changed), records).run([{"id": "a"}], execute)


@pytest.mark.asyncio
async def test_resume_rejects_rate_limit_policy_change(tmp_path: Path) -> None:
    records = tmp_path / "records"

    async def execute(case: dict, mode: str, budget: ModelCallBudget) -> dict:
        del case, mode, budget
        return {"success": True}

    await EvaluationRuntime(config(), records, max_rate_limit_retries=2).run([{"id": "a"}], execute)
    with pytest.raises(ResumeMismatchError, match="fingerprint/config"):
        await EvaluationRuntime(config(), records, max_rate_limit_retries=3).run(
            [{"id": "a"}], execute
        )


class HttpRateLimit(RuntimeError):
    def __init__(self, message: str, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.status_code = 429
        self.response = {
            "status_code": 429,
            "headers": {"Retry-After": retry_after} if retry_after else {},
        }


class HttpUnavailable(RuntimeError):
    def __init__(self, message: str = "service unavailable") -> None:
        super().__init__(message)
        self.status_code = 503
        self.response = {"status_code": 503, "headers": {}}


@pytest.mark.asyncio
async def test_budget_retries_only_the_model_call_and_honors_retry_after() -> None:
    sleeps: list[float] = []

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    attempts = 0

    async def invoke() -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise HttpRateLimit("rate limit", "2.5")
        return {
            "usage_metadata": {
                "input_tokens": 7,
                "output_tokens": 3,
                "total_tokens": 10,
            }
        }

    budget = ModelCallBudget(4, max_rate_limit_retries=2, sleeper=sleeper)
    await budget.call(invoke)
    usage = budget.snapshot()
    assert attempts == 3
    assert sleeps == [2.5, 2.5]
    assert usage["model_calls"] == 3
    assert usage["rate_limit_retries"] == 2
    assert usage["observed_total_tokens"] == 10
    # Failed provider attempts do not expose whether they consumed tokens.
    assert usage["total_tokens"] is None
    assert usage["token_usage_complete"] is False


@pytest.mark.asyncio
async def test_budget_retries_temporary_provider_outage_without_stopping_run() -> None:
    sleeps: list[float] = []
    attempts = 0

    async def invoke() -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HttpUnavailable("model is currently experiencing high demand")
        return {"usage_metadata": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}}

    budget = ModelCallBudget(
        3,
        max_transient_retries=1,
        base_backoff_seconds=0.25,
        sleeper=lambda delay: _record_sleep(sleeps, delay),
    )
    await budget.call(invoke)
    usage = budget.snapshot()
    assert attempts == 2
    assert sleeps == [0.25]
    assert usage["transient_retries"] == 1
    assert usage["rate_limit_retries"] == 0
    assert usage["model_calls"] == 2
    assert usage["total_tokens"] is None


async def _record_sleep(sleeps: list[float], delay: float) -> None:
    sleeps.append(delay)


@pytest.mark.asyncio
async def test_exact_provider_usage_is_reported_without_cost_estimates() -> None:
    async def invoke() -> dict[str, Any]:
        return {
            "raw": {
                "response_metadata": {
                    "token_usage": {
                        "prompt_tokens": 9,
                        "completion_tokens": 4,
                        "total_tokens": 13,
                    }
                }
            }
        }

    budget = ModelCallBudget(2)
    await budget.call(invoke)
    usage = budget.snapshot()
    assert usage["input_tokens"] == 9
    assert usage["output_tokens"] == 4
    assert usage["total_tokens"] == 13
    assert usage["token_usage_complete"] is True
    assert not any("cost" in key or "usd" in key for key in usage)


@pytest.mark.asyncio
async def test_daily_quota_is_pending_and_preserves_partial_usage(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    async def execute(case: dict, mode: str, budget: ModelCallBudget) -> dict:
        calls.append((case["id"], mode))

        async def successful_call() -> dict[str, Any]:
            return {
                "usage_metadata": {
                    "input_tokens": 11,
                    "output_tokens": 5,
                    "total_tokens": 16,
                }
            }

        async def quota_call() -> None:
            raise HttpRateLimit("insufficient_quota: daily quota exceeded")

        await budget.call(successful_call)
        await budget.call(quota_call)
        return {"success": True}

    report = await EvaluationRuntime(config(), tmp_path / "records").run(
        [{"id": "a"}, {"id": "b"}], execute
    )
    assert calls == [("a", "oneshot")]
    assert report["summary"]["failed"] == 0
    assert report["summary"]["pending"] == 4
    first = report["records"][0]
    assert first["status"] == "pending_quota"
    assert first["usage"]["model_calls"] == 2
    assert first["usage"]["observed_total_tokens"] == 16
    assert first["usage"]["total_tokens"] is None
    synthetic = report["records"][1:]
    assert all(record["attempts"] == 0 for record in synthetic)
    assert all(record["duration_ms"] is None for record in synthetic)

    resumed: list[tuple[str, str]] = []

    async def after_reset(case: dict, mode: str, budget: ModelCallBudget) -> dict:
        del budget
        resumed.append((case["id"], mode))
        return {"success": True}

    resumed_report = await EvaluationRuntime(config(), tmp_path / "records").run(
        [{"id": "a"}, {"id": "b"}], after_reset
    )
    assert resumed == [
        ("a", "oneshot"),
        ("a", "workflow"),
        ("b", "oneshot"),
        ("b", "workflow"),
    ]
    assert resumed_report["summary"]["completed"] == 4
    assert resumed_report["summary"]["pending"] == 0


@pytest.mark.asyncio
async def test_checkpoint_sanitizes_private_evidence_and_secrets(tmp_path: Path) -> None:
    async def execute(case: dict, mode: str, budget: ModelCallBudget) -> dict:
        del case, mode, budget
        return {
            "success": False,
            "diff": "candidate secret patch",
            "command": "python -m pytest hidden.py::test_secret",
            "acceptance": {
                "outcomes": {"hidden.py::test_secret": "failed"},
                "stdout": "github_pat_abcdefghijklmnopqrstuvwxyz123456",
                "stderr": "AIzaabcdefghijklmnopqrstuvwxyz123456789",
                "exit_code": 1,
            },
            "comparison": {
                "regressions": ["hidden.py::test_secret"],
                "missing_tests": ["hidden.py::test_missing"],
                "extra_tests": ["hidden.py::test_extra"],
                "baseline_identity_drift": ["hidden.py::test_drift"],
                "pass_to_pass_count": 4,
                "resolved": False,
            },
        }

    report = await EvaluationRuntime(config(modes=("workflow",)), tmp_path / "records").run(
        [{"id": "a"}], execute
    )
    serialized = json.dumps(report)
    assert "candidate secret patch" not in serialized
    assert "hidden.py::" not in serialized
    assert "github_pat_" not in serialized
    assert "AIza" not in serialized
    result = report["records"][0]["result"]
    assert result["success"] is False
    assert result["acceptance"] == {"exit_code": 1}
    assert result["comparison"] == {
        "regression_count": 1,
        "missing_count": 1,
        "extra_count": 1,
        "baseline_identity_drift_count": 1,
        "pass_to_pass_count": 4,
        "resolved": False,
    }


@pytest.mark.asyncio
async def test_failed_checkpoint_uses_central_secret_redaction(tmp_path: Path) -> None:
    async def execute(case: dict, mode: str, budget: ModelCallBudget) -> dict:
        del case, mode, budget
        raise RuntimeError(
            "sk-abcdefghijklmnopqrstuvwxyz123456 "
            "ghp_abcdefghijklmnopqrstuvwxyz123456 "
            "https://provider.invalid/run?api_key=query-secret"
        )

    report = await EvaluationRuntime(config(modes=("workflow",)), tmp_path / "records").run(
        [{"id": "a"}], execute
    )
    serialized = json.dumps(report)
    assert "sk-" not in serialized
    assert "ghp_" not in serialized
    assert "query-secret" not in serialized
    assert serialized.count("[REDACTED]") == 3


@pytest.mark.asyncio
async def test_long_retry_after_stops_without_sleeping(tmp_path: Path) -> None:
    sleeps: list[float] = []

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    async def execute(case: dict, mode: str, budget: ModelCallBudget) -> dict:
        del case, mode

        async def limited() -> None:
            raise HttpRateLimit("rate limit", "3600")

        await budget.call(limited)
        return {"success": True}

    report = await EvaluationRuntime(
        config(modes=("workflow",)),
        tmp_path / "records",
        max_backoff_seconds=10,
        sleeper=sleeper,
    ).run([{"id": "a"}], execute)
    assert sleeps == []
    assert report["summary"]["pending"] == 1


@pytest.mark.asyncio
async def test_ordinary_case_failure_is_isolated(tmp_path: Path) -> None:
    visited: list[str] = []

    async def execute(case: dict, mode: str, budget: ModelCallBudget) -> dict:
        del mode, budget
        visited.append(case["id"])
        if case["id"] == "b":
            raise RuntimeError("broken fixture")
        return {"success": True}

    report = await EvaluationRuntime(config(modes=("workflow",)), tmp_path / "records").run(
        [{"id": "a"}, {"id": "b"}, {"id": "c"}], execute
    )
    assert visited == ["a", "b", "c"]
    assert report["summary"]["completed"] == 2
    assert report["summary"]["failed"] == 1
    assert report["summary"]["pending"] == 0
    assert all(record["duration_ms"] is not None for record in report["records"])


@pytest.mark.asyncio
async def test_budget_cap_and_mock_model_usage_are_exactly_reported() -> None:
    budget = ModelCallBudget(1)
    model = build_agent_model(Settings(_env_file=None, model_provider="mock"), budget=budget)
    await model.plan("Fix add(a, b)", "It subtracts b")
    with pytest.raises(CallBudgetExceeded):
        await model.plan("Again", "No")
    usage = budget.snapshot()
    assert usage["model_calls"] == 1
    assert usage["successful_model_calls"] == 1
    assert usage["total_tokens"] is None
    assert usage["token_usage_complete"] is False
