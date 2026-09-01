from __future__ import annotations

import asyncio
import hmac
import json
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import ORJSONResponse, StreamingResponse
from redis.asyncio import Redis

from repopilot.config import Settings, get_settings
from repopilot.db import Database
from repopilot.events import EventBus, JobQueue
from repopilot.models import ApprovalRequest, EventView, TaskCreate, TaskStatus, TaskView
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


@app.post("/api/v1/tasks/{task_id}/approval", response_model=TaskView)
async def approve_task(
    task_id: str,
    decision: ApprovalRequest,
    request: Request,
    _: Protected,
) -> TaskView:
    _, database, events, queue, _ = resources(request)
    task = await database.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task.status != TaskStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task is not awaiting approval",
        )
    await events.publish(
        task_id,
        kind="human_decision",
        node="api",
        message="Human approved the action" if decision.approved else "Human rejected the action",
        payload={"approved": decision.approved, "feedback": decision.feedback},
    )
    await queue.enqueue(task_id, resume=decision.model_dump())
    return await database.update_task(task_id, status=TaskStatus.QUEUED)


@app.post("/api/v1/tasks/{task_id}/cancel", response_model=TaskView)
async def cancel_task(task_id: str, request: Request, _: Protected) -> TaskView:
    _, database, events, queue, _ = resources(request)
    task = await database.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
        return task
    await queue.request_cancel(task_id)
    await events.publish(
        task_id,
        kind="cancellation_requested",
        node="api",
        message="Cancellation will be enforced at the next graph boundary",
    )
    return await database.update_task(task_id, status=TaskStatus.CANCELLED)
