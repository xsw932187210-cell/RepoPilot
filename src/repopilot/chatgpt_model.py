"""Read-only Reviewer transport using user-authorized ChatGPT plan usage."""
from __future__ import annotations

from typing import Any

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from repopilot.chatgpt_auth import RESOURCE, AuthorizationError, PrivateStore, current_session
from repopilot.config import Settings
from repopilot.data_boundary import require_secret_free
from repopilot.model_calls import (
    InvalidProviderResponse,
    NonRetryableProviderError,
    ProviderRequestError,
    RateLimitError,
    TransientProviderError,
)
from repopilot.models import ReviewOutput


def stream_failure(code: str | None) -> ProviderRequestError:
    if code == "subscription_sharing_usage_limit_exceeded":
        # App limits are terminal for this task; no automatic billing fallback.
        return RateLimitError("ChatGPT app usage limit reached", daily_quota=True)
    if code in {"subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable"}:
        return TransientProviderError("ChatGPT plan usage temporarily unavailable")
    return NonRetryableProviderError(code="chatgpt_inference_rejected")


class ChatGPTTransport:
    def __init__(self, settings: Settings):
        self.storage_dir = settings.chatgpt_storage_dir
        self.model_name = settings.model_name
        self.timeout = settings.model_request_timeout_seconds
        self.max_response_bytes = settings.chatgpt_max_response_bytes

    def _auth_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=min(self.timeout, 30), follow_redirects=False)

    def _client(self, token: str) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=token, base_url=RESOURCE, max_retries=0, timeout=self.timeout,
        )

    def with_structured_output(
        self, schema: type[BaseModel], *, include_raw: bool
    ) -> _ReviewerRequest:
        if schema is not ReviewOutput or not include_raw:
            raise ValueError("ChatGPT transport supports the read-only Reviewer schema only")
        return _ReviewerRequest(self)


class _ReviewerRequest:
    def __init__(self, transport: ChatGPTTransport):
        self.transport = transport

    async def ainvoke(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        require_secret_free(messages)
        instructions = []
        inputs = []
        for message in messages:
            role, text = message.get("role"), message.get("content")
            if not isinstance(text, str):
                raise ValueError("Only text messages are supported")
            if role == "system":
                instructions.append(text)
            elif role == "user":
                inputs.append({"role": "user", "content": text})
            else:
                raise ValueError("Unsupported message role")
        if not inputs:
            raise ValueError("A user message is required")
        # Refresh happens inside the already reserved attempt, without resetting its budget.
        try:
            async with self.transport._auth_client() as auth_client:
                session = await current_session(
                    PrivateStore(self.transport.storage_dir), auth_client,
                )
        except AuthorizationError:
            raise NonRetryableProviderError(code="chatgpt_authorization_required") from None
        schema = ReviewOutput.model_json_schema()
        schema["additionalProperties"] = False
        schema["required"] = list(schema["properties"])
        for value in schema["properties"].values():
            value.pop("default", None)
        completed = None
        response_id = None
        finalized_items = {}
        received = 0
        async with self.transport._client(session["access_token"]) as client:
            async with await client.responses.create(
                model=self.transport.model_name, input=inputs,
                instructions="\n\n".join(instructions), store=False, stream=True,
                text={"format": {
                    "type": "json_schema", "name": "ReviewOutput", "schema": schema,
                    "strict": True,
                }},
                # Plan-usage preview rejects temperature and max_output_tokens.
                # The byte guard is local; it is not a server-side token/billing cap.
            ) as stream:
                async for event in stream:
                    received += len(event.model_dump_json().encode("utf-8"))
                    if received > self.transport.max_response_bytes:
                        raise ProviderRequestError(
                            "model_response_size_exceeded", "Model response exceeded local limit",
                            retryable=False, response_unknown=True, retry_kind="none",
                        )
                    response = getattr(event, "response", None)
                    identifier = getattr(response, "id", None)
                    if identifier is not None:
                        if response_id is not None and identifier != response_id:
                            raise InvalidProviderResponse()
                        response_id = identifier
                    if event.type == "response.output_item.done":
                        index = event.output_index
                        if index in finalized_items or index < 0:
                            raise InvalidProviderResponse()
                        finalized_items[index] = event.item
                    if event.type == "response.failed":
                        error = event.response.error
                        raise stream_failure(error.code if error else None)
                    if event.type in {"response.incomplete", "error"}:
                        raise NonRetryableProviderError(code="chatgpt_inference_incomplete")
                    if event.type == "response.completed":
                        if completed is not None or event.response.status != "completed":
                            raise InvalidProviderResponse()
                        completed = event.response
        if completed is None:
            raise ProviderRequestError(
                "model_stream_incomplete", "Model stream ended without completion",
                retryable=False, response_unknown=True, retry_kind="none",
            )
        # The plan-usage route may omit repeated output in its terminal frame.
        # Reconstruct only from finalized items, never from partial text deltas.
        output = completed.output or [finalized_items[i] for i in sorted(finalized_items)]
        if completed.output:
            final_by_id = {item.id: item for item in completed.output}
            for item in finalized_items.values():
                final = final_by_id.get(item.id)
                if final is None or final.model_dump() != item.model_dump():
                    raise InvalidProviderResponse()
        texts = []
        for item in output:
            if item.type == "reasoning":
                continue
            if item.type != "message" or item.role != "assistant" or item.status != "completed":
                raise InvalidProviderResponse()
            for part in item.content:
                if part.type != "output_text":
                    raise InvalidProviderResponse()
                texts.append(part.text)
        try:
            parsed = ReviewOutput.model_validate_json("".join(texts))
        except (ValidationError, ValueError, TypeError):
            raise InvalidProviderResponse() from None
        usage = completed.usage
        counters = (usage.input_tokens, usage.output_tokens, usage.total_tokens) if usage else ()
        normalized = None
        if (
            len(counters) == 3
            and all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in counters)
            and counters[0] + counters[1] == counters[2]
        ):
            normalized = dict(zip(
                ("input_tokens", "output_tokens", "total_tokens"), counters, strict=True,
            ))
        return {"parsed": parsed, "parsing_error": None, "raw": {"usage_metadata": normalized}}
