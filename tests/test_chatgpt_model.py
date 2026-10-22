from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from openai import AsyncOpenAI

from repopilot import chatgpt_auth as auth
from repopilot import chatgpt_model as provider
from repopilot.config import Settings
from repopilot.db import Database
from repopilot.llm import (
    ChatGPTReviewerModel,
    ReviewerRoutedModel,
    build_agent_model,
    model_role_profile,
)
from repopilot.model_calls import (
    CallBudgetExceeded,
    CallPolicy,
    ControlledModelCaller,
    ModelCallBudget,
    NonRetryableProviderError,
    ProviderRequestError,
    RateLimitError,
)
from repopilot.models import CodeChangeOutput, PlanOutput, SandboxResult, TaskCreate
from repopilot.repository import RepositoryContext


def session_store(tmp_path, *, expires=3600):
    store = auth.PrivateStore(tmp_path / "credentials")
    store.write("registration.json", {"client_id": "oaiapp_test"})
    store.write("session.json", {
        "client_id": "oaiapp_test", "subject": "test-subject", "issuer": auth.ISSUER,
        "ext_agent_host_id": store.host_id(), "scopes": auth.SCOPES.split(),
        "access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
        "expires_at": time.time() + expires,
    })
    return store


def completed(value=None, *, usage=True):
    text = json.dumps(value if value is not None else {
        "approved": False, "summary": "Missing test evidence", "feedback": [], "risk_level": "low",
    })
    return {
        "type": "response.completed", "sequence_number": 1,
        "response": {
            "id": "resp_fixture", "object": "response", "created_at": 1,
            "model": "gpt-6.1-sol", "status": "completed", "error": None,
            "output": [{
                "id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }],
            "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10} if usage else None,
        },
    }


@pytest.fixture
def wire(tmp_path, monkeypatch):
    store = session_store(tmp_path)
    requests, replies, configurations = [], [], []

    def respond(request):
        requests.append(request)
        assert replies, "unbudgeted or hidden SDK retry"
        status, events = replies.pop(0)
        if status != 200:
            return httpx.Response(status, json=events)
        content = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

    def client(**kwargs):
        configurations.append(kwargs.copy())
        return AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )

    monkeypatch.setattr(provider, "AsyncOpenAI", client)
    settings = Settings(
        _env_file=None, model_provider="google_cloud", model_name="gemini-3.5-flash",
        google_cloud_api_key="synthetic-cloud", review_model_provider="chatgpt",
        review_model_name="gpt-6.1-sol", chatgpt_storage_dir=store.root,
    )
    return settings, requests, replies, configurations


async def review(model):
    return await model.review("Fix addition", "-a-b\n+a+b", SandboxResult(
        command=["synthetic-no-tests-run"], exit_code=1, duration_ms=0,
    ))


@pytest.mark.asyncio
async def test_real_sdk_stream_is_complete_structured_private_and_has_exact_usage(wire):
    settings, requests, replies, configs = wire
    replies.append((200, [completed()]))
    budget = ModelCallBudget(max_calls=1)
    model = ChatGPTReviewerModel(settings.model_copy(update={"model_name": "gpt-6.1-sol"}), budget)
    result = await review(model)
    assert result.approved is False
    assert budget.snapshot()["total_tokens"] == 10
    assert budget.calls_started == 1
    assert configs[0]["max_retries"] == 0
    assert configs[0]["base_url"] == auth.RESOURCE
    request = requests[0]
    assert str(request.url) == auth.RESOURCE + "/responses"
    assert request.headers["authorization"] == "Bearer synthetic-access"
    body = json.loads(request.content)
    assert body["model"] == "gpt-6.1-sol"
    assert body["store"] is False and body["stream"] is True
    assert body["instructions"]
    assert body["input"][0]["role"] == "user"
    assert not set(body) & {"temperature", "max_output_tokens", "previous_response_id", "metadata"}
    assert body["text"]["format"]["strict"] is True
    schema = body["text"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


@pytest.mark.asyncio
async def test_terminal_frame_can_omit_output_only_after_finalized_item(wire):
    settings, requests, replies, _ = wire
    final = completed()
    item = final["response"]["output"].pop()
    replies.append((200, [
        {"type": "response.output_item.done", "output_index": 0, "item": item}, final,
    ]))
    budget = ModelCallBudget(max_calls=1)
    assert (await review(ChatGPTReviewerModel(settings, budget))).approved is False
    assert budget.snapshot()["total_tokens"] == 10
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["partial_only", "response_id", "final_item"])
async def test_partial_or_conflicting_stream_output_is_never_accepted(wire, mismatch):
    settings, _, replies, _ = wire
    final = completed()
    item = final["response"]["output"][0]
    events = []
    if mismatch == "partial_only":
        final["response"]["output"] = []
        events.append({"type": "response.output_text.delta", "delta": json.dumps({
            "approved": True, "summary": "must not accept partial text",
        })})
    elif mismatch == "response_id":
        events.append({"type": "response.created", "response": {
            "id": "resp_different", "status": "in_progress", "output": [],
        }})
    else:
        changed = json.loads(json.dumps(item))
        changed["content"][0]["text"] = "{}"
        events.append({"type": "response.output_item.done", "output_index": 0, "item": changed})
    replies.append((200, [*events, final]))
    budget = ModelCallBudget(max_calls=1)
    with pytest.raises(ProviderRequestError):
        await review(ChatGPTReviewerModel(settings, budget))
    assert budget.calls_succeeded == 0


@pytest.mark.asyncio
async def test_role_routing_keeps_one_persistent_budget_and_records_each_provider(wire, tmp_path):
    settings, requests, replies, _ = wire
    replies.append((200, [completed()]))
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'calls.db'}")
    await database.setup()
    task = await database.create_task(TaskCreate(
        repository_url="demo://buggy-calculator", issue_title="Fix addition", issue_body="fixture",
    ))
    policy = CallPolicy(max_calls=3)
    ledger = database.model_call_ledger(task.id)
    await ledger.ensure(policy)
    model = build_agent_model(settings, call_control=ControlledModelCaller(ledger, policy))
    assert isinstance(model, ReviewerRoutedModel)
    assert model.primary.call_control is model.reviewer.call_control

    class Primary:
        def with_structured_output(self, schema, *, include_raw):
            self.schema = schema
            return self

        async def ainvoke(self, _messages):
            result = PlanOutput(summary="plan", steps=["edit"]) if self.schema is PlanOutput else (
                CodeChangeOutput(summary="fixture", edits=[])
            )
            return {"parsed": result}

    model.primary.model = Primary()
    plan = await model.plan("fixture", "fixture")
    await model.propose_changes("fixture", "fixture", plan, RepositoryContext([], {}), [])
    await review(model)
    with pytest.raises(CallBudgetExceeded):
        await review(model)
    attempts = await database.list_model_call_attempts(task.id)
    assert [(r["role"], r["provider"], r["model"]) for r in attempts] == [
        ("planner", "google_cloud", "gemini-3.5-flash"),
        ("coder", "google_cloud", "gemini-3.5-flash"),
        ("reviewer", "chatgpt", "gpt-6.1-sol"),
    ]
    assert len(requests) == 1
    await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("events", "unknown"), [
    ([], True),
    ([{"type": "response.incomplete", "response": {"status": "incomplete"}}], False),
    ([completed({"approved": "invalid"})], False),
])
async def test_incomplete_or_invalid_stream_never_counts_as_success(wire, events, unknown):
    settings, requests, replies, _ = wire
    replies.append((200, events))
    budget = ModelCallBudget(max_calls=1)
    with pytest.raises(ProviderRequestError):
        await review(ChatGPTReviewerModel(settings, budget))
    assert budget.calls_succeeded == 0 and len(requests) == 1
    assert budget.snapshot()["unknown_calls"] == int(unknown)


@pytest.mark.asyncio
async def test_usage_limit_during_stream_is_terminal_without_billing_fallback(wire):
    settings, requests, replies, _ = wire
    replies.append((200, [{"type": "response.failed", "response": {
        "status": "failed", "error": {"code": "subscription_sharing_usage_limit_exceeded"},
    }}]))
    budget = ModelCallBudget(max_calls=3)
    with pytest.raises(RateLimitError) as caught:
        await review(ChatGPTReviewerModel(settings, budget))
    assert caught.value.daily_quota is True
    assert len(requests) == 1 and budget.rate_limit_retries == 0


@pytest.mark.asyncio
async def test_oversized_stream_is_unknown_terminal_and_no_free_retry(wire):
    settings, requests, replies, _ = wire
    replies.append((200, [completed({"summary": "x" * 5000})]))
    budget = ModelCallBudget(max_calls=3)
    settings = settings.model_copy(update={"chatgpt_max_response_bytes": 1024})
    with pytest.raises(ProviderRequestError) as caught:
        await review(ChatGPTReviewerModel(settings, budget))
    assert caught.value.code == "model_response_size_exceeded"
    assert budget.snapshot()["unknown_calls"] == len(requests) == 1


@pytest.mark.asyncio
async def test_http_auth_failure_is_terminal_without_sdk_retry_or_secret_echo(wire):
    settings, requests, replies, _ = wire
    replies.append((401, {"error": {"message": "synthetic-access", "type": "invalid_token"}}))
    budget = ModelCallBudget(max_calls=3)
    with pytest.raises(NonRetryableProviderError) as caught:
        await review(ChatGPTReviewerModel(settings, budget))
    assert "synthetic-access" not in str(caught.value)
    assert budget.calls_started == len(requests) == 1


def test_role_profile_exposes_actual_caps_and_mock_stays_offline(wire):
    settings, _, _, _ = wire
    roles = model_role_profile(settings)
    assert roles["coder"]["provider"] == "google_cloud"
    assert roles["reviewer"]["temperature"] is None
    assert roles["reviewer"]["server_max_output_tokens"] is None
    offline = settings.model_copy(update={"model_provider": "mock"})
    assert all(p["provider"] == "mock" for p in model_role_profile(offline).values())
    assert not isinstance(build_agent_model(offline), ReviewerRoutedModel)


@pytest.mark.asyncio
async def test_concurrent_refresh_is_serialized_and_stores_latest_rotation(tmp_path):
    store = session_store(tmp_path, expires=-1)
    requests = []

    async def respond(request):
        requests.append(request)
        await asyncio.sleep(0.02)
        assert "grant_type=refresh_token" in request.content.decode()
        assert "scope=" not in request.content.decode()
        return httpx.Response(200, json={
            "access_token": "synthetic-new-access", "refresh_token": "synthetic-new-refresh",
            "expires_in": 3600, "token_type": "Bearer",
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        first, second = await asyncio.gather(
            auth.current_session(store, client), auth.current_session(store, client),
        )
    assert len(requests) == 1
    assert first == second == store.read("session.json")
    assert first["access_token"] == "synthetic-new-access"  # noqa: S105


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "error", "cleared"), [
    (400, "invalid_grant", True), (503, "unavailable", False), (400, "invalid_client", False),
])
async def test_refresh_errors_clear_only_confirmed_unusable_tokens(
    tmp_path, status, error, cleared,
):
    store = session_store(tmp_path, expires=-1)
    prior = store.read("session.json")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        status, json={"error": error, "error_description": "do not echo private content"},
    ))) as client:
        with pytest.raises(auth.AuthorizationError):
            await auth.current_session(store, client)
    saved = store.read("session.json")
    assert ("access_token" not in saved) is cleared
    assert saved["client_id"] == prior["client_id"]
    if not cleared:
        assert saved == prior


@pytest.mark.asyncio
async def test_cancellation_during_refresh_releases_session_lock(tmp_path):
    store = session_store(tmp_path, expires=-1)
    started = asyncio.Event()

    async def respond(_request):
        started.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        task = asyncio.create_task(auth.current_session(store, client))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    with store.lock():
        assert store.read("session.json")["refresh_token"] == "synthetic-refresh"  # noqa: S105
