from __future__ import annotations

import io
import json
import logging
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from approval_helpers import approval_case
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from langsmith.run_helpers import get_tracing_context
from publication_helpers import GitHubServer, publication_case
from sqlalchemy import insert, select, update
from starlette.requests import Request
from test_reliability import MemoryRedis

from repopilot.api import app, stream_events
from repopilot.approvals import ApprovalStore, EvidenceRecord
from repopilot.candidates import ArtifactStore, Candidate, bound_resume, digest_json
from repopilot.config import Settings
from repopilot.data_boundary import (
    PublicLogFilter,
    SensitiveExecutionData,
    public_event_payload,
    public_json,
    public_result,
)
from repopilot.db import Database, EventRecord, TaskRecord
from repopilot.events import EventBus
from repopilot.github import GitHubPublisher
from repopilot.graph import GraphDependencies, build_graph
from repopilot.llm import MockAgentModel, OpenAICompatibleAgentModel
from repopilot.models import CodeChangeOutput, FileEdit, PlanOutput, TaskCreate, TaskStatus
from repopilot.publication import PublishIntentRecord
from repopilot.repository import WorkspaceManager
from repopilot.sandbox import LocalSandbox
from repopilot.security import contains_known_secret, redact_secrets
from repopilot.worker import Worker

TOKEN = "github_pat_" + "a" * 32
OPAQUE = "opaque-provider-credential"
DEMO = Path(__file__).parents[1] / "examples/buggy_calculator"


@pytest.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'privacy.db'}")
    await db.setup()
    try:
        yield db
    finally:
        await db.close()


def task_data(**overrides):
    return TaskCreate(**{
        "repository_url": "demo://buggy-calculator",
        "issue_title": "Fix add returning the wrong result",
        "issue_body": "The add function subtracts instead of adding.",
        **overrides,
    })


def api_state(monkeypatch, database, settings=None, events=None):
    for name, value in {
        "settings": settings or Settings(_env_file=None),
        "database": database,
        "queue": SimpleNamespace(enqueue=AsyncMock()),
        "events": events or EventBus(database, SimpleNamespace(publish=AsyncMock())),
        "redis": None,
    }.items():
        monkeypatch.setattr(app.state, name, value, raising=False)


@pytest.mark.parametrize("text,secret", [
    (TOKEN, TOKEN),
    ("ghp_" + "b" * 30, "ghp_" + "b" * 30),
    ("sk-proj-" + "c" * 30, "sk-proj-" + "c" * 30),
    ("AIza" + "d" * 30, "AIza" + "d" * 30),
    (f"Authorization: Bearer {OPAQUE}", OPAQUE),
    (f"Proxy-Authorization: Basic {OPAQUE}", OPAQUE),
    (f"Authorization:\n Bearer {OPAQUE}", OPAQUE),
    (f"Authorization: Bearer\n{OPAQUE}", OPAQUE),
    (f'{{"api_key": "{OPAQUE}"}}', OPAQUE),
    (f"password='{OPAQUE}'", OPAQUE),
    (f"https://user:{OPAQUE}@provider.invalid/run", OPAQUE),
    (f"postgresql+psycopg://user:{OPAQUE}@db.invalid/db", OPAQUE),
    (f"https://provider.invalid/run?access_token={OPAQUE}&ok=1", OPAQUE),
    (f"https://provider.invalid/run?api%5Fkey={OPAQUE}", OPAQUE),
    ("-----BEGIN PRIVATE KEY-----\nopaque-private-body\n-----END PRIVATE KEY-----",
     "opaque-private-body"),
])
def test_known_secrets_and_encoded_urls_are_redacted_idempotently(text, secret):
    safe = redact_secrets(text)
    assert secret not in safe and "[REDACTED" in safe
    assert redact_secrets(safe) == safe
    assert contains_known_secret(text) and not contains_known_secret(safe)


def test_nested_fields_and_public_allowlist_do_not_mutate_private_input():
    original = {
        "error_code": "transport_unavailable",
        "feedback": [{"Authorization": OPAQUE, "message": TOKEN}],
        "headers": {"X-Api-Key": OPAQUE},
        "requests": [{"candidate_id": "a" * 64, "diff_preview": "private code"}],
        "unreviewed_future_field": "private data",
    }
    before = json.dumps(original)
    projected = public_event_payload(original)
    rendered = json.dumps(projected)
    assert OPAQUE not in rendered and TOKEN not in rendered
    assert "private code" not in rendered and "private data" not in rendered
    assert projected["error_code"] == "transport_unavailable"
    assert json.dumps(original) == before
    result = public_result({"status": "failed", "research_files": {"a.py": "private code"},
                            "unreviewed_future_field": "private data", "error": "opaque error"})
    assert result == {"status": "failed", "error": "execution_failed"}
    deep = {"feedback": []}
    cursor = deep["feedback"]
    for _ in range(30):
        child = []
        cursor.append(child)
        cursor = child
    assert "[LIMIT]" in str(public_json(deep))


def test_logs_drop_tracebacks_and_preserve_error_class_without_exception_message():
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.addFilter(PublicLogFilter())
    logger = logging.Logger("boundary-unit")
    logger.addHandler(handler)
    try:
        raise RuntimeError(f"{OPAQUE}\nAuthorization: Bearer {TOKEN}")
    except RuntimeError:
        logger.exception("Provider response contains " + OPAQUE)
    logger.warning("Transport failed %s", RuntimeError(OPAQUE))
    logger.info("header %s\ninjected-line", TOKEN)
    logger.info("nested=%s", {"headers": {"custom": OPAQUE}, "api_key": OPAQUE})
    logger.info("header %(headers)s", {"headers": {"custom": OPAQUE}})
    rendered = output.getvalue()
    assert OPAQUE not in rendered and TOKEN not in rendered
    assert "Traceback" not in rendered and "RuntimeError" in rendered
    assert "execution_failed" in rendered and r"\ninjected-line" in rendered


async def test_event_storage_and_redis_publish_share_the_projection(database):
    task = await database.create_task(task_data())
    redis = SimpleNamespace(publish=AsyncMock())
    event = await EventBus(database, redis).publish(
        task.id, kind="node_update", node="planner", message=f"Failed with {TOKEN}",
        payload={"error_code": "transport_unavailable", "feedback": [{"api_key": OPAQUE}],
                 "raw": TOKEN, "new_field": "unreviewed"},
    )
    async with database.sessions() as session:
        row = await session.get(EventRecord, event.id)
        assert TOKEN not in row.message and OPAQUE not in json.dumps(row.payload)
        assert "new_field" not in row.payload
    assert redis.publish.call_args.args[1] == event.model_dump_json()
    assert event.payload["error_code"] == "transport_unavailable"


async def test_legacy_api_and_sse_filter_without_rewriting_sql(database, monkeypatch):
    task = await database.create_task(task_data())
    async with database.sessions() as session, session.begin():
        await session.execute(update(TaskRecord).where(TaskRecord.id == task.id).values(
            issue_body=TOKEN, error=f"Provider failed: {OPAQUE}",
            result={"status": "failed", "error": TOKEN, "raw": OPAQUE,
                    "research_files": {"private.py": "private code"}},
        ))
        await session.execute(insert(EventRecord).values(
            task_id=task.id, kind="task_error", node="legacy", message=TOKEN,
            payload={"error_code": "transport_unavailable", "headers": {"api_key": OPAQUE}},
            payload_schema_version=0,
        ))
    async def live(task_id):
        event = (await database.list_events(task_id))[0]
        yield event.model_copy(update={
            "id": event.id + 1, "kind": "node_update\ninjected",
            "message": TOKEN, "payload": {"feedback": [{"authorization": OPAQUE}]},
        }).model_dump_json()

    api_state(monkeypatch, database, events=SimpleNamespace(subscribe=live))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        task_response = await client.get(f"/api/v1/tasks/{task.id}")
        events_response = await client.get(f"/api/v1/tasks/{task.id}/events")
    rendered = task_response.text + events_response.text
    assert TOKEN not in rendered and OPAQUE not in rendered and "private code" not in rendered
    assert task_response.json()["error"] == "execution_failed"
    assert events_response.json()[0]["payload"]["error_code"] == "transport_unavailable"
    request = Request({"type": "http", "app": app})
    response = await stream_events(task.id, request, None)
    iterator = response.body_iterator
    history = await anext(iterator)
    live_data = await anext(iterator)
    await iterator.aclose()
    assert TOKEN not in history and OPAQUE not in history
    assert TOKEN not in live_data and OPAQUE not in live_data
    assert "\ninjected\n" not in live_data
    async with database.sessions() as session:
        row = await session.get(TaskRecord, task.id)
        event = (await session.execute(select(EventRecord))).scalar_one()
        assert row.issue_body == TOKEN and event.message == TOKEN


async def test_api_validation_does_not_echo_rejected_input(database, monkeypatch):
    api_state(monkeypatch, database)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        invalid = await client.post("/api/v1/tasks", json={
            **task_data().model_dump(), "max_iterations": TOKEN,
        })
        created = await client.post("/api/v1/tasks", json={
            **task_data().model_dump(), "issue_body": f"Reproduce bug {TOKEN}",
        })
    assert invalid.status_code == 422 and TOKEN not in invalid.text
    assert "input" not in invalid.text and "ctx" not in invalid.text
    assert created.status_code == 202 and TOKEN not in created.text
    assert TOKEN not in (await database.get_task(created.json()["id"])).issue_body


async def test_private_diff_requires_key_and_keeps_exact_bytes(database, tmp_path, monkeypatch):
    settings, view = await approval_case(database, tmp_path / "workspaces")
    api_state(monkeypatch, database, settings)
    url = f"/api/v1/tasks/{view.task_id}/candidates/{view.candidate.candidate_id}/diff"
    expected = ArtifactStore(settings.workspace_root / ".artifacts").read_diff(view.candidate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        assert (await client.get(url)).status_code == 503
        settings.api_key = "operator-test-key"
        assert (await client.get(url)).status_code == 401
        response = await client.get(url, headers={"X-API-Key": settings.api_key})
        assert response.content == expected
        assert response.headers["cache-control"] == "private, no-store"
    assert stat.S_IMODE((settings.workspace_root / ".artifacts").stat().st_mode) == 0o700


async def test_legacy_evidence_is_rejected_without_changing_its_digest(
    database, tmp_path, monkeypatch,
):
    settings, view = await approval_case(database, tmp_path / "workspaces")
    api_state(monkeypatch, database, settings)
    async with database.sessions() as session, session.begin():
        row = await session.get(EvidenceRecord, view.evidence.evidence_digest)
        row.payload = {**row.payload, "result": {**row.payload["result"], "stdout": TOKEN}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.get(f"/api/v1/tasks/{view.task_id}/approval")
    assert response.status_code == 409 and TOKEN not in response.text
    private = await ApprovalStore(database).current(view.task_id)
    assert private.evidence.evidence_digest == view.evidence.evidence_digest
    assert private.evidence.result.stdout == TOKEN


def dependencies(tmp_path, model):
    settings = Settings(_env_file=None, workspace_root=tmp_path / "workspaces",
                        demo_repository_root=DEMO, sandbox_timeout_seconds=30)
    return GraphDependencies(settings, model, WorkspaceManager(settings),
                             LocalSandbox(settings), GitHubPublisher(settings))


def graph_state(task_id):
    return {"task_id": task_id, **task_data().model_dump(),
            "node_metrics": [], "reviewer_feedback": [], "status": "queued", "iteration": 0}


async def test_graph_checkpoint_and_resume_keep_code_bytes_and_disable_external_trace(tmp_path):
    seen = []

    class ObservedModel(MockAgentModel):
        async def plan(self, title, body):
            seen.append((title, body, get_tracing_context()["enabled"]))
            return await super().plan(title, body)

    deps = dependencies(tmp_path, ObservedModel())
    private_demo = tmp_path / "private-demo"
    shutil.copytree(DEMO, private_demo)
    secret_source = f"API_KEY = '{TOKEN}'\r\nvalue = 1\r\n".encode()
    (private_demo / "config.py").write_bytes(secret_source)
    deps.settings.demo_repository_root = private_demo
    saver = InMemorySaver()
    graph = build_graph(deps, saver)
    config = {"configurable": {"thread_id": "private-recovery"}}
    initial = graph_state("private-recovery")
    initial["issue_body"] += f" Authorization: Bearer {TOKEN}"
    paused = await graph.ainvoke(initial, config=config)
    assert seen and seen[0][2] is False and TOKEN not in seen[0][1]
    async for checkpoint in saver.alist(config):
        assert TOKEN not in str(checkpoint)
    candidate = Candidate.model_validate(paused["candidate"])
    artifacts = ArtifactStore(deps.settings.workspace_root / ".artifacts")
    with artifacts.materialize(candidate) as snapshot:
        expected = (snapshot / "calculator.py").read_bytes()
        assert (snapshot / "config.py").read_bytes() == secret_source
    assert "config.py" not in paused["research_files"]
    completed = await graph.ainvoke(
        Command(resume=bound_resume(paused, approved=True, feedback="reviewed")), config=config,
    )
    assert completed["status"] == "completed"
    with artifacts.materialize(candidate) as snapshot:
        assert (snapshot / "calculator.py").read_bytes() == expected
        assert (snapshot / "config.py").read_bytes() == secret_source


async def test_sensitive_model_edit_is_never_checkpointed_or_written(tmp_path):
    class SecretModel(MockAgentModel):
        async def propose_changes(self, title, body, plan, context, feedback):
            return CodeChangeOutput(summary="bad proposal", edits=[
                FileEdit(path="calculator.py", content=f"API_KEY = '{TOKEN}'\n"),
            ])

    saver = InMemorySaver()
    graph = build_graph(dependencies(tmp_path, SecretModel()), saver)
    config = {"configurable": {"thread_id": "secret-output"}}
    result = await graph.ainvoke(graph_state("secret-output"), config=config)
    assert result["status"] == "failed" and result["error"] == "sensitive_execution_data"
    assert result.get("edits", []) == [] and result["candidate"]["generation"] == 0
    async for checkpoint in saver.alist(config):
        assert TOKEN not in str(checkpoint)


def test_repository_secret_file_is_omitted_whole_without_rewriting_source(tmp_path):
    source = tmp_path / "config.py"
    raw = f"API_KEY = '{TOKEN}'\r\nvalue = 1\r\n".encode()
    source.write_bytes(raw)
    (tmp_path / "app.py").write_bytes(b"def add(a, b):\r\n    return a - b\r\n")
    context = WorkspaceManager(Settings(_env_file=None)).inspect(tmp_path, "add", ["add"])
    assert "config.py" not in context.files and "config.py" not in context.tree
    assert source.read_bytes() == raw
    assert context.files["app.py"] == "def add(a, b):\r\n    return a - b\r\n"


async def test_provider_failure_reaches_neither_checkpoint_nor_worker_logs(
    database, tmp_path, monkeypatch, caplog,
):
    class FailingModel(MockAgentModel):
        async def plan(self, *args):
            raise RuntimeError(f"provider body {OPAQUE}\nAuthorization: Bearer {TOKEN}")

    monkeypatch.setattr("repopilot.worker.build_agent_model",
                        lambda *args, **kwargs: FailingModel())
    redis = MemoryRedis()
    redis.publish = AsyncMock()
    saver = InMemorySaver()
    worker = Worker(Settings(_env_file=None, workspace_root=tmp_path / "workspaces",
                             demo_repository_root=DEMO), database, redis, saver)
    task = await database.create_task(task_data())
    await worker.handle({"task_id": task.id})
    result = await database.get_task(task.id)
    assert result.status == TaskStatus.FAILED and result.error == "execution_failed"
    assert OPAQUE not in caplog.text and TOKEN not in caplog.text
    for event in await database.list_events(task.id):
        assert OPAQUE not in event.model_dump_json() and TOKEN not in event.model_dump_json()
    async for checkpoint in saver.alist({"configurable": {"thread_id": task.graph_thread_id}}):
        assert OPAQUE not in str(checkpoint) and TOKEN not in str(checkpoint)


async def test_provider_input_guard_runs_before_transport(monkeypatch):
    model = OpenAICompatibleAgentModel(Settings(_env_file=None, openai_api_key="unit-test-key"))
    transport_factory = AsyncMock()
    monkeypatch.setattr(type(model.model), "with_structured_output", transport_factory)
    with pytest.raises(SensitiveExecutionData):
        await model._structured_invoke(
            PlanOutput, [{"role": "user", "content": TOKEN}], "planner",
        )
    transport_factory.assert_not_called()


async def test_legacy_checkpoint_with_known_credentials_requires_operator_review(tmp_path):
    graph = build_graph(dependencies(tmp_path, MockAgentModel()), InMemorySaver())
    config = {"configurable": {"thread_id": "legacy-secret"}}
    initial = graph_state("legacy-secret")
    initial["issue_body"] = TOKEN
    # Simulate a pre-CH-06C private checkpoint, bypassing the new ingress guard.
    await graph.graph.aupdate_state(config, initial, as_node="prepare")
    with pytest.raises(SensitiveExecutionData):
        await graph.ainvoke(None, config=config)
    private = await graph.aget_state(config)
    assert private.values["issue_body"] == TOKEN


async def test_unhandled_api_error_is_a_stable_code_without_provider_body(
    database, monkeypatch, caplog,
):
    api_state(monkeypatch, database)
    monkeypatch.setattr(database, "get_task", AsyncMock(side_effect=RuntimeError(OPAQUE)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/tasks/known-task")
    assert response.status_code == 500 and response.json() == {"detail": "internal_error"}
    assert OPAQUE not in caplog.text


@pytest.mark.parametrize("already_published", [False, True])
async def test_legacy_publication_title_blocks_new_pr_but_preserves_reconciled_effect(
    database, tmp_path, already_published,
):
    settings, store, intent, _ = await publication_case(database, tmp_path / "publication")
    server = GitHubServer(intent.approved_base_sha)
    publisher = GitHubPublisher(settings, store=store, transport=httpx.MockTransport(server))
    if already_published:
        assert (await publisher.publish(intent_id=intent.publish_intent_id)).published
    async with database.sessions() as session, session.begin():
        task = await session.get(TaskRecord, intent.task_id)
        task.issue_title = f"legacy title {TOKEN}"
        row = await session.get(PublishIntentRecord, intent.publish_intent_id)
        row.payload = {**row.payload, "title": f"[RepoPilot] legacy title {TOKEN}"}
        row.payload_digest = digest_json(row.payload)
    result = await publisher.publish(intent_id=intent.publish_intent_id)
    assert all(TOKEN.encode() not in request.content for request in server.requests)
    if already_published:
        assert result.published and len(server.state["pulls"]) == 1
        assert server.posts["pr"] == 1
    else:
        assert not result.published and result.receipt.phase == "blocked"
        assert result.receipt.reason == "sensitive_publication_metadata"
        assert server.posts["pr"] == 0
