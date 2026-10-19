from __future__ import annotations

import asyncio
import hmac
import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import ORJSONResponse, StreamingResponse
from redis.asyncio import Redis

from repopilot.approval_models import (
    ApprovalDecision,
    ApprovalReceipt,
    ApprovalRequest,
    ApprovalView,
    DispatchRequest,
)
from repopilot.approvals import ApprovalConflict, ApprovalStore
from repopilot.candidates import ArtifactStore, Candidate
from repopilot.config import Settings, get_settings
from repopilot.db import Database, TaskStateConflict
from repopilot.events import EventBus, JobQueue
from repopilot.models import (
    EventView,
    NodeMetric,
    TaskCreate,
    TaskMetrics,
    TaskStatus,
    TaskView,
)
from repopilot.security import (
    SecurityError,
    parse_test_command,
    validate_branch,
    validate_repository_url,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    database = Database(settings.database_url)
    await database.setup()
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    await redis.ping()
    app.state.settings = settings
    app.state.database = database
    app.state.redis = redis
    app.state.events = EventBus(database, redis)
    app.state.queue = JobQueue(redis, settings.queue_name)
    yield
    await redis.aclose()
    await database.close()


app = FastAPI(
    title="RepoPilot",
    version="0.1.0",
    description="Recoverable, human-governed multi-agent software delivery.",
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
)


def resources(request: Request) -> tuple[Settings, Database, EventBus, JobQueue, Redis]:
    return (
        request.app.state.settings,
        request.app.state.database,
        request.app.state.events,
        request.app.state.queue,
        request.app.state.redis,
    )


async def require_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    expected = request.app.state.settings.api_key
    if expected and (not x_api_key or not hmac.compare_digest(expected, x_api_key)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


Protected = Annotated[None, Depends(require_api_key)]


def summarize_task_metrics(
    task: TaskView,
    model_calls: Mapping[str, int | float | bool | str | None] | None = None,
) -> TaskMetrics:
    metrics = [
        NodeMetric.model_validate(metric) for metric in (task.result or {}).get("node_metrics", [])
    ]
    runs: Counter[str] = Counter()
    durations: defaultdict[str, int] = defaultdict(int)
    for metric in metrics:
        runs[metric.node] += 1
        durations[metric.node] += metric.duration_ms
    wall_time_ms = max(0, int((task.updated_at - task.created_at).total_seconds() * 1_000))
    sandbox_time_ms = sum(metric.duration_ms for metric in metrics if metric.node == "test_runner")
    retrieval = (task.result or {}).get("retrieval", {})
    selected_files = retrieval.get("selected_files", [])
    call_metrics = model_calls or {}
    return TaskMetrics(
        task_id=task.id,
        status=task.status,
        wall_time_ms=wall_time_ms,
        node_time_ms=sum(metric.duration_ms for metric in metrics),
        node_runs=dict(runs),
        node_duration_ms=dict(durations),
        iterations=max((metric.iteration for metric in metrics), default=0),
        sandbox_time_ms=sandbox_time_ms,
        retrieval_strategy=retrieval.get("strategy"),
        retrieval_candidate_files=int(retrieval.get("candidate_count", 0)),
        retrieval_selected_files=len(selected_files),
        retrieval_context_chars=int(retrieval.get("selected_chars", 0)),
        model_call_policy_version=(
            str(call_metrics["call_control_version"])
            if call_metrics.get("call_control_version")
            else None
        ),
        model_calls_max=int(call_metrics.get("max_model_calls") or 0),
        model_calls_reserved=int(call_metrics.get("reserved_calls") or 0),
        model_calls_started=int(call_metrics.get("started_calls") or 0),
        model_calls_succeeded=int(call_metrics.get("successful_model_calls") or 0),
        model_calls_failed=int(call_metrics.get("failed_calls") or 0),
        model_calls_unknown=int(call_metrics.get("unknown_calls") or 0),
        model_calls_pending_reservation=int(call_metrics.get("pending_reserved_calls") or 0),
        model_calls_pending_response=int(call_metrics.get("pending_started_calls") or 0),
        model_rate_limit_retries=int(call_metrics.get("rate_limit_retries") or 0),
        model_transient_retries=int(call_metrics.get("transient_retries") or 0),
        model_fallback_calls=int(call_metrics.get("fallback_calls") or 0),
        model_backoff_seconds=float(call_metrics.get("backoff_seconds") or 0),
        model_token_usage_complete=bool(call_metrics.get("token_usage_complete", False)),
        model_observed_input_tokens=int(call_metrics.get("observed_input_tokens") or 0),
        model_observed_output_tokens=int(call_metrics.get("observed_output_tokens") or 0),
        model_observed_total_tokens=int(call_metrics.get("observed_total_tokens") or 0),
    )


@app.get("/health")
async def health(request: Request) -> dict[str, str]:
    _, _, _, _, redis = resources(request)
    await redis.ping()
    return {"status": "ok"}


@app.post("/api/v1/tasks", response_model=TaskView, status_code=status.HTTP_202_ACCEPTED)
async def create_task(data: TaskCreate, request: Request, _: Protected) -> TaskView:
    _, database, events, queue, _ = resources(request)
    try:
        validate_repository_url(data.repository_url)
        validate_branch(data.base_branch)
        parse_test_command(data.test_command)
    except SecurityError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    task = await database.create_task(data)
    await events.publish(
        task.id,
        kind="task_created",
        node="api",
        message="Task accepted and queued",
        payload={"repository_url": task.repository_url, "issue_title": task.issue_title},
    )
    await queue.enqueue(task.id)
    return task


@app.get("/api/v1/tasks/{task_id}", response_model=TaskView)
async def get_task(task_id: str, request: Request, _: Protected) -> TaskView:
    _, database, _, _, _ = resources(request)
    task = await database.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


@app.get("/api/v1/tasks/{task_id}/metrics", response_model=TaskMetrics)
async def get_task_metrics(task_id: str, request: Request, _: Protected) -> TaskMetrics:
    _, database, _, _, _ = resources(request)
    task = await database.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    model_calls = await database.get_model_call_metrics(task_id)
    return summarize_task_metrics(task, model_calls)


@app.get("/api/v1/tasks/{task_id}/events", response_model=list[EventView])
async def get_events(
    task_id: str,
    request: Request,
    _: Protected,
    after_id: int = 0,
) -> list[EventView]:
    _, database, _, _, _ = resources(request)
    if await database.get_task(task_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return await database.list_events(task_id, after_id=after_id)


@app.get("/api/v1/tasks/{task_id}/stream")
async def stream_events(task_id: str, request: Request, _: Protected) -> StreamingResponse:
    _, database, events, _, _ = resources(request)
    if await database.get_task(task_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    async def generate():
        last_id = 0
        for event in await database.list_events(task_id):
            last_id = event.id
            yield f"id: {event.id}\nevent: {event.kind}\ndata: {event.model_dump_json()}\n\n"
        subscriber = events.subscribe(task_id).__aiter__()
        while True:
            try:
                raw = await asyncio.wait_for(subscriber.__anext__(), timeout=15)
            except TimeoutError:
                yield ": heartbeat\n\n"
                continue
            except StopAsyncIteration:
                return
            payload = json.loads(raw)
            if int(payload.get("id", 0)) <= last_id:
                continue
            last_id = int(payload["id"])
            yield f"id: {last_id}\nevent: {payload['kind']}\ndata: {raw}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


def approval_error(error: KeyError | ApprovalConflict) -> HTTPException:
    return HTTPException(
        status_code=404 if isinstance(error, KeyError) else 409,
        detail="Task or candidate not found" if isinstance(error, KeyError) else error.code,
    )


@app.get("/api/v1/tasks/{task_id}/approval", response_model=ApprovalView)
async def get_approval(task_id: str, request: Request, _: Protected) -> ApprovalView:
    _, database, _, _, _ = resources(request)
    try:
        return await ApprovalStore(database).current(task_id)
    except (KeyError, ApprovalConflict) as error:
        raise approval_error(error) from error


@app.get("/api/v1/tasks/{task_id}/candidates/{candidate_id}", response_model=Candidate)
async def get_candidate(
    task_id: str,
    candidate_id: str,
    request: Request,
    _: Protected,
) -> Candidate:
    _, database, _, _, _ = resources(request)
    try:
        return await ApprovalStore(database).candidate(task_id, candidate_id)
    except KeyError as error:
        raise approval_error(error) from error


@app.get("/api/v1/tasks/{task_id}/candidates/{candidate_id}/diff")
async def get_candidate_diff(
    task_id: str,
    candidate_id: str,
    request: Request,
    _: Protected,
) -> Response:
    settings, database, _, _, _ = resources(request)
    try:
        candidate = await ApprovalStore(database).candidate(task_id, candidate_id)
        diff = await asyncio.to_thread(
            ArtifactStore(settings.workspace_root / ".artifacts").read_diff, candidate
        )
    except KeyError as error:
        raise approval_error(error) from error
    except (SecurityError, OSError) as error:
        raise HTTPException(status_code=503, detail="approval_artifact_unavailable") from error
    return Response(diff, media_type="text/plain", headers={"ETag": f'"{candidate.diff_digest}"'})


async def deliver_approval(
    task_id: str,
    decision: ApprovalDecision,
    store: ApprovalStore,
    queue: JobQueue,
) -> ApprovalDecision:
    if decision.dispatch_state not in {"pending", "enqueued"}:
        return decision
    try:
        decision = await store.dispatchable(task_id, decision.decision_id)
    except ApprovalConflict:
        # An identical request can race with Worker consumption or cancellation.
        return await store.decision(task_id, decision.decision_id)
    try:
        await queue.enqueue(task_id, resume=decision.resume_payload())
    except Exception:
        # The decision is committed. A delivery error must be visible and retryable,
        # including an unknown enqueue outcome after a transport disconnection.
        await store.delivery_attempt(decision.decision_id, succeeded=False)
    else:
        await store.delivery_attempt(decision.decision_id, succeeded=True)
    return await store.decision(task_id, decision.decision_id)


async def approval_receipt(
    task_id: str,
    decision: ApprovalDecision,
    database: Database,
    response: Response,
) -> ApprovalReceipt:
    task = await database.get_task(task_id)
    assert task is not None
    if decision.dispatch_state == "pending":
        response.status_code = 202
    return ApprovalReceipt(task_id=task_id, task_status=task.status, decision=decision)


@app.post("/api/v1/tasks/{task_id}/approval", response_model=ApprovalReceipt)
async def approve_task(
    task_id: str,
    decision: ApprovalRequest,
    request: Request,
    response: Response,
    _: Protected,
) -> ApprovalReceipt:
    settings, database, _, queue, _ = resources(request)
    store = ApprovalStore(database)
    try:
        candidate = await store.candidate(task_id, decision.candidate_id)
        artifacts = ArtifactStore(settings.workspace_root / ".artifacts")

        def verify_artifact() -> None:
            artifacts.read_diff(candidate)
            with artifacts.materialize(candidate):
                pass

        await asyncio.to_thread(verify_artifact)
        saved = await store.decide(
            task_id,
            decision,
            actor="api-key-operator" if settings.api_key else "local-operator",
        )
    except (KeyError, ApprovalConflict) as error:
        raise approval_error(error) from error
    except (SecurityError, OSError) as error:
        raise HTTPException(status_code=503, detail="approval_artifact_unavailable") from error
    saved = await deliver_approval(task_id, saved, store, queue)
    return await approval_receipt(task_id, saved, database, response)


@app.post("/api/v1/tasks/{task_id}/approval/dispatch", response_model=ApprovalReceipt)
async def retry_approval_dispatch(
    task_id: str,
    data: DispatchRequest,
    request: Request,
    response: Response,
    _: Protected,
) -> ApprovalReceipt:
    _, database, _, queue, _ = resources(request)
    store = ApprovalStore(database)
    try:
        decision = await store.dispatchable(task_id, str(data.decision_id))
    except (KeyError, ApprovalConflict) as error:
        raise approval_error(error) from error
    saved = await deliver_approval(task_id, decision, store, queue)
    return await approval_receipt(task_id, saved, database, response)


@app.post("/api/v1/tasks/{task_id}/cancel", response_model=TaskView)
async def cancel_task(task_id: str, request: Request, _: Protected) -> TaskView:
    _, database, events, queue, _ = resources(request)
    task = await database.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
        return task
    try:
        cancelled = await database.transition_task(
            task_id,
            expected_status=task.status,
            expected_version=task.state_version,
            status=TaskStatus.CANCELLED,
        )
    except TaskStateConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    await queue.request_cancel(task_id)
    await events.publish(
        task_id,
        kind="cancellation_requested",
        node="api",
        message="Cancellation will be enforced at the next graph boundary",
    )
    return cancelled
