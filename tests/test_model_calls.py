from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from repopilot.db import Database
from repopilot.llm import BudgetedAgentModel
from repopilot.model_calls import (
    BackoffBudgetExceeded,
    CallBudgetExceeded,
    CallIdentity,
    CallPolicy,
    ControlledModelCaller,
    InvalidProviderResponse,
    NonRetryableProviderError,
    RateLimitError,
    RetryWaitLimitExceeded,
    TokenBudgetExceeded,
)
from repopilot.models import (
    CodeChangeOutput,
    PlanOutput,
    ReviewOutput,
    SandboxResult,
    TaskCreate,
)
from repopilot.repository import RepositoryContext


class HttpError(RuntimeError):
    def __init__(self, status_code: int, retry_after: str | None = None) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.response = {
            "status_code": status_code,
            "headers": {"Retry-After": retry_after} if retry_after is not None else {},
        }


async def persistent_caller(
    tmp_path: Path,
    *,
    policy: CallPolicy,
    sleeper=None,
) -> tuple[Database, str, ControlledModelCaller]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'calls.db'}")
    await database.setup()
    task = await database.create_task(
        TaskCreate(
            repository_url="demo://buggy-calculator",
            issue_title="Exercise controlled provider calls",
            issue_body="Use a deterministic transport and inspect the persistent ledger.",
        )
    )
    ledger = database.model_call_ledger(task.id)
    await ledger.ensure(policy)
    return database, task.id, ControlledModelCaller(
        ledger,
        policy,
        sleeper=sleeper or asyncio.sleep,
    )


def identity(**overrides: Any) -> CallIdentity:
    values = {
        "provider": "fixture-provider",
        "model": "fixture-model-2026-09-21",
        "adapter_version": "fixture-adapter-v1",
        "request_schema_version": "fixture-response-v1",
    }
    values.update(overrides)
    return CallIdentity(**values)


@pytest.mark.asyncio
async def test_success_is_reserved_before_transport_and_records_exact_identity_and_usage(
    tmp_path: Path,
) -> None:
    policy = CallPolicy(max_calls=2, max_total_tokens=20, max_output_tokens=123)
    database, task_id, caller = await persistent_caller(tmp_path, policy=policy)
    seen_reserved: list[int] = []

    async def transport() -> dict[str, Any]:
        snapshot = await database.get_model_call_metrics(task_id)
        assert snapshot is not None
        seen_reserved.append(int(snapshot["reserved_calls"]))
        assert snapshot["started_calls"] == 1
        assert snapshot["pending_started_calls"] == 1
        return {
            "usage_metadata": {
                "input_tokens": 7,
                "output_tokens": 3,
                "total_tokens": 10,
            }
        }

    await caller.call(
        transport,
        call_identity=identity(),
        call_role="planner",
    )
    snapshot = await database.get_model_call_metrics(task_id)
    attempts = await database.list_model_call_attempts(task_id)
    await database.close()

    assert seen_reserved == [1]
    assert snapshot is not None
    assert snapshot["reserved_calls"] == 1
    assert snapshot["started_calls"] == 1
    assert snapshot["successful_model_calls"] == 1
    assert snapshot["failed_calls"] == 0
    assert snapshot["unknown_calls"] == 0
    assert snapshot["token_usage_complete"] is True
    assert snapshot["total_tokens"] == 10
    assert snapshot["max_output_tokens"] == 123
    assert attempts[0] | {
        "provider": "fixture-provider",
        "model": "fixture-model-2026-09-21",
        "adapter_version": "fixture-adapter-v1",
        "request_schema_version": "fixture-response-v1",
        "role": "planner",
        "status": "succeeded",
    } == attempts[0]


@pytest.mark.asyncio
async def test_exact_call_and_trusted_token_limits_block_before_transport(tmp_path: Path) -> None:
    policy = CallPolicy(max_calls=2, max_total_tokens=5)
    database, task_id, caller = await persistent_caller(tmp_path, policy=policy)
    transports = 0

    async def transport() -> dict[str, Any]:
        nonlocal transports
        transports += 1
        return {
            "usage_metadata": {
                "input_tokens": 3,
                "output_tokens": 2,
                "total_tokens": 5,
            }
        }

    await caller.call(transport, call_identity=identity(), call_role="coder")
    with pytest.raises(TokenBudgetExceeded, match=r"5/5"):
        await caller.call(transport, call_identity=identity(), call_role="reviewer")
    assert transports == 1

    snapshot = await database.get_model_call_metrics(task_id)
    await database.close()
    assert snapshot is not None
    assert snapshot["reserved_calls"] == 1
    assert snapshot["observed_total_tokens"] == 5


@pytest.mark.asyncio
async def test_exact_call_limit_is_consumed_and_next_call_never_reaches_transport(
    tmp_path: Path,
) -> None:
    policy = CallPolicy(max_calls=1)
    database, task_id, caller = await persistent_caller(tmp_path, policy=policy)
    calls = 0

    async def transport() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"ok": "done"}

    await caller.call(transport, call_identity=identity(), call_role="planner")
    with pytest.raises(CallBudgetExceeded, match=r"1/1"):
        await caller.call(transport, call_identity=identity(), call_role="coder")
    snapshot = await database.get_model_call_metrics(task_id)
    await database.close()
    assert calls == 1
    assert snapshot is not None
    assert snapshot["reserved_calls"] == 1


@pytest.mark.asyncio
async def test_missing_usage_disables_token_gate_but_keeps_call_and_output_caps(
    tmp_path: Path,
) -> None:
    policy = CallPolicy(max_calls=2, max_total_tokens=1, max_output_tokens=17)
    database, task_id, caller = await persistent_caller(tmp_path, policy=policy)
    calls = 0

    async def transport() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"result": "no provider usage metadata"}

    await caller.call(transport, call_identity=identity(), call_role="planner")
    await caller.call(transport, call_identity=identity(), call_role="coder")
    with pytest.raises(CallBudgetExceeded):
        await caller.call(transport, call_identity=identity(), call_role="reviewer")
    snapshot = await database.get_model_call_metrics(task_id)
    await database.close()

    assert calls == 2
    assert snapshot is not None
    assert snapshot["token_usage_complete"] is False
    assert snapshot["max_output_tokens"] == 17


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retry_after", "expected_sleeps"),
    [
        ("2.5", [2.5, 2.5]),
        (None, [1.0, 2.0]),
        ("not-a-delay", [1.0, 2.0]),
    ],
)
async def test_consecutive_429_uses_valid_retry_after_or_bounded_exponential_fallback(
    tmp_path: Path,
    retry_after: str | None,
    expected_sleeps: list[float],
) -> None:
    sleeps: list[float] = []

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    policy = CallPolicy(max_calls=3, max_rate_limit_retries=2)
    database, task_id, caller = await persistent_caller(
        tmp_path, policy=policy, sleeper=sleeper
    )
    calls = 0

    async def transport() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise HttpError(429, retry_after)
        return {"usage_metadata": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}

    await caller.call(transport, call_identity=identity(), call_role="coder")
    snapshot = await database.get_model_call_metrics(task_id)
    attempts = await database.list_model_call_attempts(task_id)
    await database.close()

    assert calls == 3
    assert sleeps == expected_sleeps
    assert snapshot is not None
    assert snapshot["rate_limit_retries"] == 2
    assert [attempt["status"] for attempt in attempts] == ["failed", "failed", "succeeded"]


@pytest.mark.asyncio
async def test_5xx_connection_reset_and_timeout_are_bounded_and_connection_results_unknown(
    tmp_path: Path,
) -> None:
    sleeps: list[float] = []

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    policy = CallPolicy(
        max_calls=6,
        max_transient_retries=1,
        request_timeout_seconds=0.01,
        base_backoff_seconds=0,
    )
    database, task_id, caller = await persistent_caller(
        tmp_path, policy=policy, sleeper=sleeper
    )

    five_xx_calls = 0

    async def five_xx() -> dict[str, str]:
        nonlocal five_xx_calls
        five_xx_calls += 1
        if five_xx_calls == 1:
            raise HttpError(503)
        return {"ok": "5xx recovered"}

    connection_calls = 0

    async def connection() -> dict[str, str]:
        nonlocal connection_calls
        connection_calls += 1
        if connection_calls == 1:
            raise ConnectionResetError("connection reset")
        return {"ok": "connection recovered"}

    timeout_calls = 0

    async def timeout() -> dict[str, str]:
        nonlocal timeout_calls
        timeout_calls += 1
        if timeout_calls == 1:
            await asyncio.Future()
        return {"ok": "timeout recovered"}

    await caller.call(five_xx, call_identity=identity(), call_role="planner")
    await caller.call(connection, call_identity=identity(), call_role="coder")
    await caller.call(timeout, call_identity=identity(), call_role="reviewer")
    snapshot = await database.get_model_call_metrics(task_id)
    attempts = await database.list_model_call_attempts(task_id)
    await database.close()

    assert (five_xx_calls, connection_calls, timeout_calls) == (2, 2, 2)
    assert snapshot is not None
    assert snapshot["reserved_calls"] == 6
    assert snapshot["failed_calls"] == 1
    assert snapshot["unknown_calls"] == 2
    assert snapshot["successful_model_calls"] == 3
    assert snapshot["transient_retries"] == 3
    assert [attempt["error_code"] for attempt in attempts[::2]] == [
        "model_provider_transient_failure",
        "model_provider_response_unknown",
        "model_provider_response_unknown",
    ]


@pytest.mark.asyncio
async def test_invalid_response_and_nonretryable_4xx_stop_without_another_transport(
    tmp_path: Path,
) -> None:
    policy = CallPolicy(max_calls=4)
    database, task_id, caller = await persistent_caller(tmp_path, policy=policy)
    calls = 0

    async def malformed() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"bad": "shape"}

    def validator(_: object) -> None:
        raise InvalidProviderResponse()

    with pytest.raises(NonRetryableProviderError) as invalid:
        await caller.call(
            malformed,
            call_identity=identity(),
            call_role="planner",
            response_validator=validator,
        )
    assert invalid.value.code == "model_invalid_response"

    async def rejected() -> None:
        nonlocal calls
        calls += 1
        raise HttpError(400)

    with pytest.raises(NonRetryableProviderError) as rejection:
        await caller.call(rejected, call_identity=identity(), call_role="coder")
    assert rejection.value.code == "model_provider_rejected_request"
    snapshot = await database.get_model_call_metrics(task_id)
    await database.close()
    assert calls == 2
    assert snapshot is not None
    assert snapshot["failed_calls"] == 2


@pytest.mark.asyncio
async def test_retry_count_single_wait_and_cumulative_backoff_limits(tmp_path: Path) -> None:
    sleeps: list[float] = []

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    single_wait_policy = CallPolicy(max_calls=3, max_retry_wait_seconds=5)
    database, task_id, caller = await persistent_caller(
        tmp_path, policy=single_wait_policy, sleeper=sleeper
    )

    async def long_wait() -> None:
        raise HttpError(429, "30")

    with pytest.raises(RetryWaitLimitExceeded):
        await caller.call(long_wait, call_identity=identity(), call_role="planner")
    assert sleeps == []
    single = await database.get_model_call_metrics(task_id)
    await database.close()
    assert single is not None
    assert single["reserved_calls"] == 1

    cumulative_policy = CallPolicy(
        max_calls=4,
        max_transient_retries=3,
        base_backoff_seconds=1,
        max_retry_wait_seconds=10,
        max_total_backoff_seconds=1,
    )
    database, task_id, caller = await persistent_caller(
        tmp_path, policy=cumulative_policy, sleeper=sleeper
    )

    async def unavailable() -> None:
        raise HttpError(503)

    with pytest.raises(BackoffBudgetExceeded):
        await caller.call(unavailable, call_identity=identity(), call_role="coder")
    cumulative = await database.get_model_call_metrics(task_id)
    await database.close()
    assert sleeps == [1]
    assert cumulative is not None
    assert cumulative["reserved_calls"] == 2
    assert cumulative["transient_retries"] == 1
    assert cumulative["backoff_seconds"] == 1


@pytest.mark.asyncio
async def test_retry_limit_stops_continuous_provider_failure(tmp_path: Path) -> None:
    policy = CallPolicy(
        max_calls=5,
        max_rate_limit_retries=1,
        base_backoff_seconds=0,
    )
    database, task_id, caller = await persistent_caller(
        tmp_path, policy=policy, sleeper=lambda _: asyncio.sleep(0)
    )
    calls = 0

    async def limited() -> None:
        nonlocal calls
        calls += 1
        raise HttpError(429)

    with pytest.raises(RateLimitError):
        await caller.call(limited, call_identity=identity(), call_role="planner")
    snapshot = await database.get_model_call_metrics(task_id)
    await database.close()
    assert calls == 2
    assert snapshot is not None
    assert snapshot["reserved_calls"] == 2
    assert snapshot["rate_limit_retries"] == 1


@pytest.mark.asyncio
async def test_fallback_identity_is_explicit_and_separate_in_metrics(tmp_path: Path) -> None:
    policy = CallPolicy(max_calls=1)
    database, task_id, caller = await persistent_caller(tmp_path, policy=policy)

    async def transport() -> dict[str, str]:
        return {"ok": "fallback"}

    await caller.call(
        transport,
        call_identity=identity(
            provider="backup-provider",
            model="backup-model",
            is_fallback=True,
            fallback_from_provider="fixture-provider",
            fallback_from_model="fixture-model-2026-09-21",
        ),
        call_role="coder",
    )
    snapshot = await database.get_model_call_metrics(task_id)
    attempts = await database.list_model_call_attempts(task_id)
    await database.close()
    assert snapshot is not None
    assert snapshot["fallback_calls"] == 1
    assert attempts[0]["provider"] == "backup-provider"
    assert attempts[0]["fallback_from_model"] == "fixture-model-2026-09-21"


@pytest.mark.asyncio
async def test_restart_keeps_reservation_made_before_transport(tmp_path: Path) -> None:
    policy = CallPolicy(max_calls=1)
    database, task_id, _ = await persistent_caller(tmp_path, policy=policy)
    first = database.model_call_ledger(task_id)
    await first.ensure(policy)
    await first.reserve(
        logical_call_id="00000000-0000-0000-0000-000000000001",
        role="planner",
        identity=identity(),
        retry_index=0,
    )

    restarted = database.model_call_ledger(task_id)
    await restarted.ensure(policy)
    caller = ControlledModelCaller(restarted, policy)
    transports = 0

    async def transport() -> None:
        nonlocal transports
        transports += 1

    with pytest.raises(CallBudgetExceeded):
        await caller.call(transport, call_identity=identity(), call_role="planner")
    snapshot = await restarted.snapshot()
    await database.close()
    assert transports == 0
    assert snapshot["reserved_calls"] == 1
    assert snapshot["pending_reserved_calls"] == 1


@pytest.mark.asyncio
async def test_restart_marks_possible_sent_response_unknown_without_free_retry(
    tmp_path: Path,
) -> None:
    policy = CallPolicy(max_calls=1)
    database, task_id, _ = await persistent_caller(tmp_path, policy=policy)
    first = database.model_call_ledger(task_id)
    await first.ensure(policy)
    reservation = await first.reserve(
        logical_call_id="00000000-0000-0000-0000-000000000002",
        role="coder",
        identity=identity(),
        retry_index=0,
    )
    await first.mark_started(reservation.attempt_id)

    restarted = database.model_call_ledger(task_id)
    await restarted.ensure(policy)
    assert await restarted.reconcile_incomplete() == 1
    caller = ControlledModelCaller(restarted, policy)
    transports = 0

    async def transport() -> None:
        nonlocal transports
        transports += 1

    with pytest.raises(CallBudgetExceeded):
        await caller.call(transport, call_identity=identity(), call_role="coder")
    snapshot = await restarted.snapshot()
    await database.close()
    assert transports == 0
    assert snapshot["unknown_calls"] == 1
    assert snapshot["pending_started_calls"] == 0


class CountingAgentModel:
    def __init__(self) -> None:
        self.roles: list[str] = []

    async def plan(self, issue_title: str, issue_body: str) -> PlanOutput:
        del issue_title, issue_body
        self.roles.append("planner")
        return PlanOutput(summary="plan", steps=["edit"])

    async def propose_changes(
        self,
        issue_title: str,
        issue_body: str,
        plan: PlanOutput,
        context: RepositoryContext,
        reviewer_feedback: list[str],
    ) -> CodeChangeOutput:
        del issue_title, issue_body, plan, context, reviewer_feedback
        self.roles.append("coder")
        return CodeChangeOutput(summary="proposal", edits=[])

    async def review(
        self,
        issue_title: str,
        diff: str,
        test_result: SandboxResult,
    ) -> ReviewOutput:
        del issue_title, diff, test_result
        self.roles.append("reviewer")
        return ReviewOutput(approved=True, summary="reviewed")


@pytest.mark.asyncio
async def test_planner_coder_policy_correction_and_reviewer_share_one_task_budget(
    tmp_path: Path,
) -> None:
    policy = CallPolicy(max_calls=4)
    database, task_id, caller = await persistent_caller(tmp_path, policy=policy)
    underlying = CountingAgentModel()
    model = BudgetedAgentModel(underlying, caller)
    plan = await model.plan("issue", "body")
    context = RepositoryContext(tree=[], files={})
    await model.propose_changes("issue", "body", plan, context, [])
    await model.propose_changes("issue", "body", plan, context, ["policy correction"])
    await model.review(
        "issue",
        "diff",
        SandboxResult(command=["pytest"], exit_code=0, duration_ms=1),
    )
    with pytest.raises(CallBudgetExceeded):
        await model.review(
            "issue",
            "diff",
            SandboxResult(command=["pytest"], exit_code=0, duration_ms=1),
        )
    attempts = await database.list_model_call_attempts(task_id)
    await database.close()
    assert underlying.roles == ["planner", "coder", "coder", "reviewer"]
    assert [attempt["role"] for attempt in attempts] == [
        "planner",
        "coder",
        "coder",
        "reviewer",
    ]
