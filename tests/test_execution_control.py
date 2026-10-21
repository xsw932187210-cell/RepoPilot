from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from approval_helpers import approval_case, decision_request
from langgraph.checkpoint.memory import InMemorySaver
from publication_helpers import GitHubServer, publication_case

from repopilot.api import app
from repopilot.approvals import ApprovalConflict, ApprovalStore
from repopilot.config import Settings
from repopilot.db import Database, TaskRecord, TaskStateConflict
from repopilot.execution import CleanupFailed, ExecutionControl, ExecutionStopped, drain_thread
from repopilot.github import GitHubPublisher
from repopilot.llm import BudgetedAgentModel, MockAgentModel
from repopilot.model_calls import (
    CallPolicy,
    ControlledModelCaller,
    InMemoryCallLedger,
    RateLimitError,
)
from repopilot.models import TaskCreate, TaskStatus
from repopilot.sandbox import DockerSandbox
from repopilot.worker import Worker


@pytest.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'control.db'}")
    await db.setup()
    try:
        yield db
    finally:
        await db.close()


def task_data(**kwargs):
    return TaskCreate(
        repository_url="demo://buggy-calculator",
        issue_title="Fix add returning the wrong result",
        issue_body="The add(a, b) function subtracts b instead of adding it.",
        **kwargs,
    )


async def running(database, **kwargs):
    task = await database.create_task(task_data(**kwargs))
    return await database.transition_task(
        task.id,
        expected_status=task.status,
        expected_version=task.state_version,
        status=TaskStatus.RUNNING,
    )


async def test_api_running_cancel_is_durable_request_even_when_redis_is_down(database, monkeypatch):
    task = await running(database)
    for name, value in dict(
        settings=Settings(_env_file=None),
        database=database,
        queue=SimpleNamespace(request_cancel=AsyncMock(side_effect=ConnectionError)),
        events=None,
        redis=None,
    ).items():
        monkeypatch.setattr(app.state, name, value, raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/api/v1/tasks/{task.id}/cancel")
        repeated = await client.post(f"/api/v1/tasks/{task.id}/cancel")
    assert response.status_code == repeated.status_code == 202
    assert response.json()["status"] == "cancelling"
    assert response.json()["stopped_at"] is None
    assert repeated.json()["state_version"] == task.state_version + 1
    assert [e.kind for e in await database.list_events(task.id)] == ["cancellation_requested"]
    with pytest.raises(TaskStateConflict):
        await database.transition_task(
            task.id,
            expected_status=task.status,
            expected_version=task.state_version,
            status=TaskStatus.COMPLETED,
        )


async def test_idle_cancel_confirms_and_invalidates_waiting_approval(database, tmp_path):
    queued = await database.create_task(task_data())
    stopped = await database.request_stop(queued.id)
    assert stopped.status == TaskStatus.CANCELLED and stopped.stopped_at
    _, view = await approval_case(database, tmp_path / "workspaces")
    stopped = await database.request_stop(view.task_id)
    assert stopped.status == TaskStatus.CANCELLED and stopped.stopped_at
    with pytest.raises(ApprovalConflict, match="approval_cancelled"):
        await ApprovalStore(database).decide(view.task_id, decision_request(view), actor="operator")


async def test_cleanup_failure_retains_identity_and_cannot_claim_stopped(database):
    task = await running(database)
    resource = {"kind": "docker_container", "id": "a" * 64}
    await database.execution_resources(task.id, task.state_version, [resource])
    requested = await database.request_stop(task.id)
    pending = await database.confirm_stop(
        task.id,
        expected_version=requested.state_version,
        cleanup_pending=[{**resource, "action": "remove", "error": "APIError"}],
    )
    assert pending.status == TaskStatus.CANCELLING and pending.stopped_at is None
    assert pending.cleanup_pending[0]["id"] == resource["id"]
    reopened = Database(database.url)
    try:
        restored = await reopened.get_task(task.id)
        assert restored.execution_resources == [resource]
        assert restored.cleanup_pending == pending.cleanup_pending
    finally:
        await reopened.close()
    with pytest.raises(TaskStateConflict):
        await database.confirm_stop(task.id, expected_version=requested.state_version - 1)


@pytest.mark.parametrize(
    "queue_counts,approval_counts", [(False, False), (True, False), (False, True), (True, True)]
)
async def test_fake_clock_accounts_phases_and_preserves_remaining_on_reopen(
    database, monkeypatch, queue_counts, approval_counts
):
    now = [datetime(2026, 10, 22, tzinfo=UTC)]
    monkeypatch.setattr("repopilot.db.utcnow", lambda: now[0])
    task = await database.create_task(
        task_data(
            execution_timeout_seconds=100,
            deadline_includes_queue=queue_counts,
            deadline_includes_approval=approval_counts,
        )
    )
    now[0] += timedelta(seconds=7)
    task = await database.transition_task(
        task.id,
        expected_status=task.status,
        expected_version=task.state_version,
        status=TaskStatus.RUNNING,
    )
    now[0] += timedelta(seconds=11)
    task = await database.transition_task(
        task.id,
        expected_status=task.status,
        expected_version=task.state_version,
        status=TaskStatus.AWAITING_APPROVAL,
    )
    now[0] += timedelta(seconds=13)
    reopened = Database(database.url)
    try:
        task = await reopened.get_task(task.id)
        assert task.execution_elapsed_seconds == 11
        assert task.queue_elapsed_seconds == 7
        assert task.remaining_seconds == 100 - 11 - 7 * queue_counts - 13 * approval_counts
        assert bool(task.deadline_at) == approval_counts
    finally:
        await reopened.close()


async def test_approval_deadline_denies_decision_and_sweep_confirms_idle_stop(database, tmp_path):
    _, view = await approval_case(database, tmp_path / "workspaces")
    async with database.sessions() as session, session.begin():
        row = await session.get(TaskRecord, view.task_id)
        row.deadline_includes_approval = True
        row.execution_timeout_seconds = 1
        row.phase_started_at = datetime.now(UTC) - timedelta(seconds=5)
    with pytest.raises(ApprovalConflict, match="task_deadline_exceeded"):
        await ApprovalStore(database).decide(view.task_id, decision_request(view), actor="operator")
    await database.expire_waiting_tasks()
    stopped = await database.get_task(view.task_id)
    assert stopped.status == TaskStatus.FAILED and stopped.stopped_at
    assert stopped.stop_reason == "deadline_exceeded"


async def test_expired_fake_clock_does_not_reserve_or_send_another_model_call():
    clock = [0.0]
    control = ExecutionControl("task", 5, clock=lambda: clock[0])
    policy = CallPolicy(max_calls=5)
    ledger = InMemoryCallLedger(policy)
    caller = ControlledModelCaller(ledger, policy)
    transport = AsyncMock(return_value={})
    with control.bind():
        assert control.timeout(90) == 5
        await caller.call(transport)
        clock[0] = 5
        with pytest.raises(ExecutionStopped, match="deadline_exceeded"):
            await caller.call(transport)
    assert transport.await_count == 1
    assert (await ledger.snapshot())["reserved_calls"] == 1


async def test_cancelling_provider_wait_closes_local_transport_and_keeps_usage_unknown():
    entered, closed = asyncio.Event(), asyncio.Event()

    async def transport():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    policy = CallPolicy(max_calls=3)
    ledger = InMemoryCallLedger(policy)
    caller = ControlledModelCaller(ledger, policy)
    control = ExecutionControl("model", 30)
    with control.bind():
        operation = asyncio.create_task(caller.call(transport))
        await asyncio.wait_for(entered.wait(), 2)
        control.stop()
        with pytest.raises(ExecutionStopped):
            await asyncio.wait_for(operation, 2)
    metrics = await ledger.snapshot()
    assert closed.is_set()
    assert metrics["unknown_calls"] == metrics["started_calls"] == 1
    assert not metrics["token_usage_complete"]


async def test_cancel_during_backoff_sends_no_retry():
    sleeping = asyncio.Event()

    async def sleeper(_):
        sleeping.set()
        await asyncio.Event().wait()

    transport = AsyncMock(side_effect=RateLimitError("rate limit"))
    policy = CallPolicy(max_calls=5)
    caller = ControlledModelCaller(InMemoryCallLedger(policy), policy, sleeper=sleeper)
    control = ExecutionControl("backoff", 30)
    with control.bind():
        operation = asyncio.create_task(caller.call(transport))
        await asyncio.wait_for(sleeping.wait(), 2)
        control.stop()
        with pytest.raises(ExecutionStopped):
            await asyncio.wait_for(operation, 2)
    assert transport.await_count == 1


async def test_abort_scope_does_not_cancel_task():
    control = ExecutionControl("lost-lease", 30)
    control.abort_attempt()
    with pytest.raises(ExecutionStopped) as error:
        control.check()
    assert error.value.scope == "attempt" and error.value.reason == "attempt_aborted"


async def test_cancelled_thread_is_joined_before_caller_finishes():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def write():
        started.set()
        release.wait(2)
        finished.set()

    operation = asyncio.create_task(drain_thread(write))
    await asyncio.to_thread(started.wait, 2)
    operation.cancel()
    await asyncio.sleep(0)
    assert not operation.done()
    operation.cancel()
    await asyncio.sleep(0)
    assert not operation.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert finished.is_set()


@pytest.mark.parametrize("remove_fails", [False, True])
async def test_container_stop_joins_thread_and_retains_cleanup_failure(
    tmp_path, monkeypatch, remove_fails
):
    from docker.errors import APIError
    from test_sandbox import SnapshotClient, SnapshotContainer

    entered, killed = threading.Event(), threading.Event()

    class Container(SnapshotContainer):
        def wait(self, timeout):
            entered.set()
            assert killed.wait(3)
            return {"StatusCode": 137}

        def kill(self):
            super().kill()
            killed.set()

        def remove(self, *, force):
            if remove_fails:
                raise APIError("fixture cleanup unavailable")
            super().remove(force=force)

    container = Container()
    monkeypatch.setattr("repopilot.sandbox.docker.from_env", lambda **_: SnapshotClient(container))
    control = ExecutionControl("docker", 30)
    with control.bind():
        operation = asyncio.create_task(
            control.run(
                DockerSandbox(Settings(_env_file=None)).run(tmp_path, "python -m pytest -q")
            )
        )
        assert await asyncio.to_thread(entered.wait, 2)
        control.stop()
        with pytest.raises(CleanupFailed if remove_fails else ExecutionStopped):
            await asyncio.wait_for(operation, 3)
    assert container.killed
    assert bool(control.resources) == remove_fails
    assert container.removed != remove_fails


@pytest.mark.parametrize("stage", ["blob", "pr"])
async def test_inflight_publication_cancellation_reconciles_effect_without_new_writes(
    database, tmp_path, stage
):
    settings, store, intent, _ = await publication_case(database, tmp_path / "workspaces")
    remote = GitHubServer(intent.approved_base_sha)
    entered = asyncio.Event()

    async def transport(request):
        response = await remote(request)
        target = "git/blobs" if stage == "blob" else "pulls"
        if request.method == "POST" and request.url.path.endswith(target):
            entered.set()
            await asyncio.Event().wait()  # Remote effect happened; response has not arrived.
        return response

    control = ExecutionControl(intent.task_id, 30)
    publisher = GitHubPublisher(settings, httpx.MockTransport(transport), store=store)

    async def cancelled():
        return bool(control.reason)

    with control.bind():
        operation = asyncio.create_task(
            publisher.publish(intent_id=intent.publish_intent_id, cancelled=cancelled)
        )
        await asyncio.wait_for(entered.wait(), 5)
        requested = await database.request_stop(intent.task_id)
        assert requested.status == TaskStatus.CANCELLING and requested.stopped_at is None
        control.stop()
        result = await asyncio.wait_for(operation, 3)
    assert result.receipt.cancel_requested
    assert (
        result.receipt.steps["pr" if stage == "pr" else next(iter(result.receipt.steps))]
        == "confirmed"
    )
    assert remote.posts[stage] == 1
    assert len(remote.state["pulls"]) == (1 if stage == "pr" else 0)
    stopped = await database.confirm_stop(intent.task_id, expected_version=requested.state_version)
    assert stopped.stopped_at
    assert (await store.load(intent.publish_intent_id))[1].remote_ids == result.receipt.remote_ids


async def test_worker_confirms_model_stop_only_after_request_wait_ends(
    database, tmp_path, monkeypatch
):
    from test_reliability import MemoryRedis

    entered, closed = asyncio.Event(), asyncio.Event()

    class SlowModel(MockAgentModel):
        async def plan(self, *args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

    monkeypatch.setattr(
        "repopilot.worker.build_agent_model",
        lambda settings, call_control: BudgetedAgentModel(SlowModel(), call_control),
    )
    redis = MemoryRedis()
    redis.publish = AsyncMock()
    worker = Worker(
        Settings(
            _env_file=None, workspace_root=tmp_path / "workspaces", cancellation_poll_seconds=0.01
        ),
        database,
        redis,
        InMemorySaver(),
    )
    task = await database.create_task(task_data())
    operation = asyncio.create_task(worker.handle({"task_id": task.id}))
    await asyncio.wait_for(entered.wait(), 5)
    requested = await database.request_stop(task.id)
    assert requested.status == TaskStatus.CANCELLING and not requested.stopped_at
    await asyncio.wait_for(operation, 5)
    stopped = await database.get_task(task.id)
    assert closed.is_set() and stopped.status == TaskStatus.CANCELLED and stopped.stopped_at
    assert (await database.get_model_call_metrics(task.id))["unknown_calls"] == 1


async def test_expiry_during_provider_wait_is_unknown_and_never_retried():
    clock = [0.0]
    entered = asyncio.Event()

    async def transport():
        entered.set()
        await asyncio.Event().wait()

    policy = CallPolicy(max_calls=5)
    ledger = InMemoryCallLedger(policy)
    caller = ControlledModelCaller(ledger, policy)
    control = ExecutionControl("deadline", 10, clock=lambda: clock[0])
    with control.bind():
        operation = asyncio.create_task(caller.call(transport))
        await asyncio.wait_for(entered.wait(), 2)
        clock[0] = 10
        assert control.remaining() == 0
        with pytest.raises(ExecutionStopped, match="deadline_exceeded"):
            await asyncio.wait_for(operation, 2)
    metrics = await ledger.snapshot()
    assert metrics["unknown_calls"] == metrics["reserved_calls"] == 1
    assert metrics["transient_retries"] == 0


async def test_restart_retains_unconfirmed_stop_instead_of_inventing_confirmation(
    database, tmp_path
):
    task = await running(database)
    pending = await database.request_stop(task.id)
    worker = Worker(Settings(_env_file=None, workspace_root=tmp_path), database, None, None)
    await worker.report_pending_stops()
    restored = await database.get_task(task.id)
    assert restored.status == TaskStatus.CANCELLING and restored.stopped_at is None
    assert restored.state_version == pending.state_version
    assert restored.cleanup_pending[0]["error"] == "worker_restart"


async def test_waiting_deadline_sweeper_runs_independently_of_execution(
    database, tmp_path, monkeypatch
):
    waiting = await database.create_task(
        task_data(execution_timeout_seconds=1, deadline_includes_queue=True)
    )
    async with database.sessions() as session, session.begin():
        row = await session.get(TaskRecord, waiting.id)
        row.phase_started_at = datetime.now(UTC) - timedelta(seconds=5)
    worker = Worker(Settings(_env_file=None, workspace_root=tmp_path), database, None, None)
    stopped = asyncio.Event()
    original = database.request_stop

    async def observed_stop(task_id, reason):
        result = await original(task_id, reason)
        stopped.set()
        return result

    monkeypatch.setattr(database, "request_stop", observed_stop)
    sweeper = asyncio.create_task(worker._sweep_deadlines())
    try:
        await asyncio.wait_for(stopped.wait(), 2)
    finally:
        sweeper.cancel()
        await asyncio.gather(sweeper, return_exceptions=True)
    assert (await database.get_task(waiting.id)).stop_reason == "deadline_exceeded"


async def test_generic_terminal_transition_rejects_live_execution_resources(database):
    task = await running(database)
    resource = {"kind": "docker_container", "id": "b" * 64}
    await database.execution_resources(task.id, task.state_version, [resource])
    with pytest.raises(CleanupFailed):
        await database.transition_task(
            task.id,
            expected_status=task.status,
            expected_version=task.state_version,
            status=TaskStatus.FAILED,
        )
    assert (await database.get_task(task.id)).stopped_at is None


async def test_stop_notifications_reuse_committed_event_ids(database):
    import json

    from repopilot.events import EventBus

    task = await database.create_task(task_data())
    await database.request_stop(task.id)
    redis = SimpleNamespace(publish=AsyncMock())
    bus = EventBus(database, redis)
    await bus.notify_control(task.id)
    await bus.notify_control(task.id)
    events = await database.list_events(task.id)
    ids = [json.loads(call.args[1])["id"] for call in redis.publish.await_args_list]
    assert len(events) == 2
    assert ids == [events[0].id, events[1].id] * 2
