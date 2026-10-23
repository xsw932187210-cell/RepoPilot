"""Text-only structured transport for Google Cloud Express API keys.

The native SDK gets the explicit Cloud key, Cloud endpoint and one-attempt retry
policy. Neither ambient Developer API keys nor project/ADC settings choose the
backend. The shared agent layer owns prompts, validation, tracing and budgets.
"""

from __future__ import annotations

from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from repopilot.config import Settings
from repopilot.data_boundary import require_secret_free
from repopilot.model_calls import InvalidProviderResponse

CLOUD_ENDPOINT = "https://aiplatform.googleapis.com/"


def token_usage(usage: types.GenerateContentResponseUsageMetadata | None) -> dict[str, int] | None:
    """Normalize complete counters; an unexplained total remains unknown."""
    if usage is None:
        return None
    prompt, candidate, total = (
        usage.prompt_token_count, usage.candidates_token_count, usage.total_token_count
    )
    if any(
        not isinstance(n, int) or isinstance(n, bool) or n < 0
        for n in (prompt, candidate, total)
    ):
        return None
    thoughts = usage.thoughts_token_count
    if thoughts is None:
        # Omitted thoughts are safe only if the explicit total needs no addition.
        thoughts = 0
    if not isinstance(thoughts, int) or isinstance(thoughts, bool) or thoughts < 0:
        return None
    output = candidate + thoughts
    if prompt + output != total:
        return None
    return {"input_tokens": prompt, "output_tokens": output, "total_tokens": total}


class GoogleCloudTransport:
    def __init__(self, settings: Settings):
        if not settings.google_cloud_api_key:
            raise ValueError("GOOGLE_CLOUD_API_KEY is required when MODEL_PROVIDER=google_cloud")
        self._key = settings.google_cloud_api_key
        self.model_name = settings.model_name
        self.temperature = settings.model_temperature
        self.max_output_tokens = settings.model_max_output_tokens
        self.timeout_ms = max(1, int(settings.model_request_timeout_seconds * 1000))

    def _client(self) -> genai.Client:
        # No process-environment mutation, implicit key selection or ADC loading.
        return genai.Client(
            vertexai=True,
            api_key=self._key,
            http_options=types.HttpOptions(
                base_url=CLOUD_ENDPOINT,
                api_version="v1beta1",
                timeout=self.timeout_ms,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    def with_structured_output(
        self, schema: type[BaseModel], *, include_raw: bool
    ) -> _StructuredRequest:
        if not include_raw:
            raise ValueError("Provider usage requires the raw response envelope")
        return _StructuredRequest(self, schema)


class _StructuredRequest:
    def __init__(self, transport: GoogleCloudTransport, schema: type[BaseModel]):
        self.transport = transport
        self.schema = schema

    async def ainvoke(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        require_secret_free(messages)
        instructions: list[str] = []
        contents: list[types.Content] = []
        for message in messages:
            role, text = message.get("role"), message.get("content")
            if not isinstance(text, str):
                raise ValueError("Only text messages are supported")
            if role == "system":
                instructions.append(text)
            elif role in {"user", "assistant"}:
                contents.append(types.Content(
                    role="model" if role == "assistant" else "user",
                    parts=[types.Part.from_text(text=text)],
                ))
            else:
                raise ValueError("Unsupported message role")
        if not contents:
            raise ValueError("A user message is required")
        client = self.transport._client()
        try:
            async with client.aio as async_client:
                response = await async_client.models.generate_content(
                    model=self.transport.model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction="\n\n".join(instructions) or None,
                        temperature=self.transport.temperature,
                        max_output_tokens=self.transport.max_output_tokens,
                        candidate_count=1,
                        response_mime_type="application/json",
                        response_schema=self.schema,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(
                            disable=True
                        ),
                    ),
                )
        finally:
            client.close()
        if not response.candidates or len(response.candidates) != 1:
            raise InvalidProviderResponse()
        candidate = response.candidates[0]
        if candidate.finish_reason != types.FinishReason.STOP or candidate.content is None:
            raise InvalidProviderResponse()
        text = "".join(
            part.text for part in candidate.content.parts or []
            if part.text is not None and not part.thought
        )
        try:
            parsed = self.schema.model_validate_json(text)
        except (ValidationError, ValueError, TypeError):
            raise InvalidProviderResponse() from None
        return {
            "parsed": parsed,
            "parsing_error": None,
            "raw": {"usage_metadata": token_usage(response.usage_metadata)},
        }
