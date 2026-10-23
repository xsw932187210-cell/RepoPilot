from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest
from google import genai
from google.genai import errors, types

from repopilot.config import Settings
from repopilot.google_cloud import CLOUD_ENDPOINT, GoogleCloudTransport, token_usage
from repopilot.llm import GoogleCloudAgentModel, build_agent_model
from repopilot.model_calls import (
    CallBudgetExceeded,
    ModelCallBudget,
    NonRetryableProviderError,
    RateLimitError,
    TransientProviderError,
    classify_provider_error,
)
from repopilot.models import PlanOutput, SandboxResult
from repopilot.policy_feedback import content_sha256
from repopilot.repository import RepositoryContext


def cloud_settings(**overrides: Any) -> Settings:
    values = {
        "model_provider": "google_cloud",
        "model_name": "gemini-3.5-flash",
        "google_cloud_api_key": "explicit-cloud-fixture-key",
        "model_temperature": 1.0,
        "model_request_timeout_seconds": 7,
        "model_max_output_tokens": 2048,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def response_body(value: object, **overrides: Any) -> dict[str, Any]:
    result = {
        "candidates": [{
            "content": {"role": "model", "parts": [{"text": json.dumps(value)}]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {
            "promptTokenCount": 7, "candidatesTokenCount": 3,
            "thoughtsTokenCount": 2, "totalTokenCount": 12,
        },
    }
    result.update(overrides)
    return result


@pytest.fixture
def wire(monkeypatch):
    """Use the real SDK serialization/error/retry path with an offline HTTP transport."""
    requests: list[httpx.Request] = []
    replies: list[tuple[int, dict[str, Any]]] = []
    options: list[types.HttpOptions] = []
    real_client = genai.Client

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert replies, "unexpected SDK retry or unbudgeted request"
        status, body = replies.pop(0)
        return httpx.Response(status, json=body)

    def client(**kwargs):
        configured = kwargs["http_options"]
        options.append(configured)
        kwargs["http_options"] = configured.model_copy(update={
            "async_client_args": {"transport": httpx.MockTransport(handle)},
        })
        return real_client(**kwargs)

    monkeypatch.setattr(genai, "Client", client)
    # Explicit Cloud configuration must win over all ambient Google settings.
    monkeypatch.setenv("GOOGLE_API_KEY", "other-developer-fixture-key")
    monkeypatch.setenv("GEMINI_API_KEY", "other-gemini-fixture-key")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "ambient-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "ambient-location")
    monkeypatch.setenv("GOOGLE_VERTEX_BASE_URL", "https://wrong.example.test")
    return requests, replies, options


@pytest.mark.asyncio
async def test_three_roles_use_cloud_key_schema_usage_and_one_shared_budget(wire, monkeypatch):
    requests, replies, options = wire
    source = "def add(a, b):\n    return a - b\n"
    replies.extend([
        (200, response_body({"summary": "fix addition", "steps": ["change operator"]})),
        (200, response_body({"summary": "fix addition", "edits": [{
            "path": "calc.py", "content": source.replace("a - b", "a + b"),
            "action": "write", "expected_content_sha256": content_sha256(source),
        }]})),
        (200, response_body({"approved": False, "summary": "No test run supplied"})),
    ])
    snapshots = []

    async def persist(snapshot):
        snapshots.append(dict(snapshot))

    budget = ModelCallBudget(max_calls=3, state_hook=persist)
    model = build_agent_model(cloud_settings(), budget=budget)
    assert isinstance(model, GoogleCloudAgentModel)
    plan = await model.plan("Fix addition", "add should return the sum")
    change = await model.propose_changes(
        "Fix addition", "add should return the sum", plan,
        RepositoryContext(["calc.py"], {"calc.py": source}, ("calc.py",)), [],
    )
    review = await model.review(
        "Fix addition", "-return a-b\n+return a+b",
        SandboxResult(command=["synthetic-no-tests-run"], exit_code=1, duration_ms=0),
    )
    with pytest.raises(CallBudgetExceeded):
        await model.plan("fourth request", "must not reach HTTP")
    assert len(requests) == 3
    assert not replies
    assert change.edits[0].expected_content_sha256 == content_sha256(source)
    assert review.approved is False
    snapshot = budget.snapshot()
    assert snapshot["reserved_calls"] == snapshot["started_calls"] == 3
    assert snapshot["successful_model_calls"] == 3
    assert snapshot["observed_total_tokens"] == 36
    assert snapshot["observed_output_tokens"] == 15  # includes thoughts
    assert snapshot["token_usage_complete"] is True
    assert any(s["pending_reserved_calls"] == 1 for s in snapshots)
    assert any(s["pending_started_calls"] == 1 for s in snapshots)
    for request, configured in zip(requests, options, strict=True):
        assert str(request.url).startswith(CLOUD_ENDPOINT + "v1beta1/publishers/google/models/")
        assert "/projects/" not in str(request.url)
        assert "key=" not in str(request.url)
        assert request.headers["x-goog-api-key"] == "explicit-cloud-fixture-key"
        assert "authorization" not in request.headers
        assert configured.retry_options.attempts == 1
        assert configured.timeout == 7000
        body = json.loads(request.content)
        config = body["generationConfig"]
        assert config["temperature"] == 1.0
        assert config["maxOutputTokens"] == 2048
        assert config["candidateCount"] == 1
        assert config["responseMimeType"] == "application/json"
        assert config["responseSchema"]["type"] == "OBJECT"
        assert body["systemInstruction"]["parts"][0]["text"]
        assert "tools" not in body
    assert os.environ["GOOGLE_API_KEY"] == "other-developer-fixture-key"


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "exception"), [
    (429, RateLimitError), (503, TransientProviderError), (401, NonRetryableProviderError),
])
async def test_native_errors_have_no_hidden_sdk_retries(wire, status, exception):
    requests, replies, _ = wire
    replies.append((status, {"error": {"code": status, "message": "fixture failure"}}))
    budget = ModelCallBudget(max_calls=3, max_rate_limit_retries=0, max_transient_retries=0)
    model = GoogleCloudAgentModel(cloud_settings(), budget=budget)
    with pytest.raises(exception):
        await model.plan("fixture", "fixture")
    assert len(requests) == budget.calls_started == 1
    assert budget.snapshot()["failed_calls"] == 1


@pytest.mark.asyncio
async def test_controlled_retry_reserves_a_second_actual_http_attempt(wire):
    requests, replies, _ = wire
    replies.extend([
        (503, {"error": {"code": 503, "message": "fixture failure"}}),
        (200, response_body({"summary": "ok", "steps": ["inspect"]})),
    ])
    budget = ModelCallBudget(max_calls=2, max_transient_retries=1, base_backoff_seconds=0)
    model = GoogleCloudAgentModel(cloud_settings(), budget=budget)
    assert (await model.plan("fixture", "fixture")).summary == "ok"
    assert len(requests) == budget.calls_started == 2
    assert budget.snapshot()["failed_calls"] == budget.calls_succeeded == 1
    assert budget.transient_retries == 1


@pytest.mark.parametrize(("status", "exception"), [
    (429, RateLimitError), (503, TransientProviderError), (401, NonRetryableProviderError),
])
def test_google_integer_error_code_without_an_http_response(status, exception):
    error = errors.APIError(status, {"error": {"message": "fixture"}})
    assert isinstance(classify_provider_error(error), exception)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    response_body({"summary": "no required steps"}),
    response_body({}, candidates=[]),
    response_body({}, candidates=[{"finishReason": "MAX_TOKENS"}]),
    response_body({}, candidates=[{"finishReason": "SAFETY"}]),
    response_body({}, candidates=[{
        "finishReason": "STOP", "content": {"parts": [{"text": "not JSON"}]},
    }]),
])
async def test_invalid_blocked_and_truncated_responses_are_terminal(wire, body):
    requests, replies, _ = wire
    replies.append((200, body))
    model = GoogleCloudAgentModel(cloud_settings())
    with pytest.raises(NonRetryableProviderError) as caught:
        await model.plan("fixture", "fixture")
    assert caught.value.code == "model_invalid_response"
    assert len(requests) == 1


@pytest.mark.parametrize(("counters", "expected"), [
    (None, None),
    ({"prompt_token_count": 7, "candidates_token_count": 3, "total_token_count": 10},
     {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}),
    ({"prompt_token_count": 7, "candidates_token_count": 3,
      "thoughts_token_count": 2, "total_token_count": 12},
     {"input_tokens": 7, "output_tokens": 5, "total_tokens": 12}),
    ({"prompt_token_count": 7, "total_token_count": 10}, None),
    ({"prompt_token_count": 7, "candidates_token_count": 3, "total_token_count": 12}, None),
    ({"prompt_token_count": -1, "candidates_token_count": 3, "total_token_count": 2}, None),
])
def test_usage_is_complete_and_explicit_or_unknown(counters, expected):
    usage = types.GenerateContentResponseUsageMetadata(**counters) if counters else None
    assert token_usage(usage) == expected


@pytest.mark.asyncio
async def test_missing_usage_is_unknown_without_losing_call_limit(wire):
    requests, replies, _ = wire
    replies.append((200, response_body(
        {"summary": "ok", "steps": ["inspect"]}, usageMetadata=None,
    )))
    budget = ModelCallBudget(max_calls=1, max_total_tokens=1)
    model = GoogleCloudAgentModel(cloud_settings(), budget=budget)
    await model.plan("fixture", "fixture")
    with pytest.raises(CallBudgetExceeded):
        await model.plan("fixture", "fixture")
    assert len(requests) == 1
    assert budget.snapshot()["token_usage_complete"] is False
    assert budget.snapshot()["total_tokens"] is None


def test_cloud_key_is_required_and_not_in_settings_repr():
    with pytest.raises(ValueError, match="GOOGLE_CLOUD_API_KEY"):
        GoogleCloudTransport(cloud_settings(google_cloud_api_key=""))
    assert "explicit-cloud-fixture-key" not in repr(cloud_settings())


@pytest.mark.asyncio
async def test_secret_input_is_rejected_before_transport(wire):
    requests, _, _ = wire
    model = GoogleCloudAgentModel(cloud_settings())
    with pytest.raises(ValueError):
        await model.plan("fixture", "AIza" + "X" * 35)
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize("messages", [
    [{"role": "tool", "content": "no tools"}],
    [{"role": "user", "content": [{"text": "not text"}]}],
    [{"role": "system", "content": "no user content"}],
])
async def test_transport_rejects_unsupported_messages_before_http(wire, messages):
    requests, _, _ = wire
    structured = GoogleCloudTransport(cloud_settings()).with_structured_output(
        PlanOutput, include_raw=True,
    )
    with pytest.raises(ValueError):
        await structured.ainvoke(messages)
    assert not requests
