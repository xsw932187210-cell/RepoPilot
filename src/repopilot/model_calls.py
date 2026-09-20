from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, Protocol, TypeVar

CALL_CONTROL_VERSION = "provider-call-control-v1"
EVALUATION_BUDGET_VERSION = "evaluation-call-budget-v2"
DEFAULT_ADAPTER_VERSION = "generic-async-v1"

_T = TypeVar("_T")
Sleeper = Callable[[float], Awaitable[None]]
UsageExtractor = Callable[[object], tuple[int, int, int] | None]
ResponseValidator = Callable[[object], None]
StateHook = Callable[[Mapping[str, Any]], Awaitable[None]]


class CallStatus(StrEnum):
    RESERVED = "reserved"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ModelCallControlError(RuntimeError):
    """A stable, public failure from the controlled model-call boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CallBudgetExceeded(ModelCallControlError):
    def __init__(self, used: int, limit: int) -> None:
        self.used = used
        self.limit = limit
        super().__init__(
            "model_call_budget_exhausted",
            f"model call budget exhausted ({used}/{limit})",
        )


class TokenBudgetExceeded(ModelCallControlError):
    def __init__(self, used: int, limit: int) -> None:
        self.used = used
        self.limit = limit
        super().__init__(
            "model_token_budget_exhausted",
            f"trusted model token budget exhausted ({used}/{limit})",
        )


class CallPolicyMismatch(ModelCallControlError):
    def __init__(self) -> None:
        super().__init__(
            "model_call_policy_mismatch",
            "persisted model call policy does not match the requested policy",
        )


class CallLedgerStateConflict(ModelCallControlError):
    def __init__(self, attempt_id: str, expected: CallStatus) -> None:
        super().__init__(
            "model_call_ledger_conflict",
            f"model call attempt {attempt_id} is not in expected state {expected.value}",
        )


class RetryWaitLimitExceeded(ModelCallControlError):
    def __init__(self, delay: float, limit: float) -> None:
        self.delay = delay
        self.limit = limit
        super().__init__(
            "model_retry_wait_limit_exceeded",
            f"provider retry wait {delay:g}s exceeds the {limit:g}s limit",
        )


class BackoffBudgetExceeded(ModelCallControlError):
    def __init__(self, delay: float, remaining: float) -> None:
        self.delay = delay
        self.remaining = remaining
        super().__init__(
            "model_backoff_budget_exhausted",
            f"provider retry wait {delay:g}s exceeds remaining backoff budget {remaining:g}s",
        )


class InvalidProviderResponse(ModelCallControlError):
    def __init__(self, message: str = "model provider returned an invalid response") -> None:
        super().__init__("model_invalid_response", message)


class ProviderRequestError(ModelCallControlError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        response_unknown: bool,
        retry_after_seconds: float | None = None,
        daily_quota: bool = False,
        retry_kind: str = "transient",
    ) -> None:
        super().__init__(code, message)
        self.retryable = retryable
        self.response_unknown = response_unknown
        self.retry_after_seconds = retry_after_seconds
        self.daily_quota = daily_quota
        self.retry_kind = retry_kind


class RateLimitError(ProviderRequestError):
    """Normalized provider rate-limit signal shared by online and evaluation calls."""

    def __init__(
        self,
        message: str = "model provider rate limit",
        *,
        retry_after_seconds: float | None = None,
        daily_quota: bool = False,
    ) -> None:
        super().__init__(
            "model_provider_quota_exhausted" if daily_quota else "model_provider_rate_limit",
            message,
            retryable=not daily_quota,
            response_unknown=False,
            retry_after_seconds=retry_after_seconds,
            daily_quota=daily_quota,
            retry_kind="rate_limit",
        )


class TransientProviderError(ProviderRequestError):
    """Normalized retryable provider outage, connection failure, or timeout."""

    def __init__(
        self,
        message: str = "model provider temporarily unavailable",
        *,
        retry_after_seconds: float | None = None,
        response_unknown: bool = False,
        code: str = "model_provider_transient_failure",
    ) -> None:
        super().__init__(
            code,
            message,
            retryable=True,
            response_unknown=response_unknown,
            retry_after_seconds=retry_after_seconds,
            retry_kind="transient",
        )


class NonRetryableProviderError(ProviderRequestError):
    def __init__(self, code: str = "model_provider_rejected_request") -> None:
        super().__init__(
            code,
            "model provider rejected the request",
            retryable=False,
            response_unknown=False,
            retry_kind="none",
        )


@dataclass(frozen=True, slots=True)
class CallUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int

    def __post_init__(self) -> None:
        values = (self.input_tokens, self.output_tokens, self.total_tokens)
        if any(value < 0 for value in values):
            raise ValueError("token usage cannot be negative")


@dataclass(frozen=True, slots=True)
class CallIdentity:
    provider: str
    model: str
    adapter_version: str
    request_schema_version: str
    is_fallback: bool = False
    fallback_from_provider: str | None = None
    fallback_from_model: str | None = None

    def __post_init__(self) -> None:
        required = (
            self.provider,
            self.model,
            self.adapter_version,
            self.request_schema_version,
        )
        if any(not value.strip() for value in required):
            raise ValueError("provider, model, adapter, and request schema must be named")
        fallback_named = bool(self.fallback_from_provider or self.fallback_from_model)
        if self.is_fallback != fallback_named:
            raise ValueError("fallback calls must name their original provider or model")


@dataclass(frozen=True, slots=True)
class CallPolicy:
    max_calls: int
    request_timeout_seconds: float = 90.0
    max_rate_limit_retries: int = 2
    max_transient_retries: int = 2
    base_backoff_seconds: float = 1.0
    max_retry_wait_seconds: float = 30.0
    max_total_backoff_seconds: float = 60.0
    max_total_tokens: int | None = None
    max_output_tokens: int = 4_096
    version: str = CALL_CONTROL_VERSION

    def __post_init__(self) -> None:
        if self.max_calls < 1:
            raise ValueError("max_calls must be positive")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if self.max_rate_limit_retries < 0 or self.max_transient_retries < 0:
            raise ValueError("retry limits cannot be negative")
        durations = (
            self.base_backoff_seconds,
            self.max_retry_wait_seconds,
            self.max_total_backoff_seconds,
        )
        if any(duration < 0 for duration in durations):
            raise ValueError("backoff durations cannot be negative")
        if self.max_total_tokens is not None and self.max_total_tokens < 1:
            raise ValueError("max_total_tokens must be positive when enabled")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        if not self.version.strip():
            raise ValueError("call policy version must be named")

    def persisted_values(self) -> dict[str, int | float | str | None]:
        return {
            "policy_version": self.version,
            "max_calls": self.max_calls,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_rate_limit_retries": self.max_rate_limit_retries,
            "max_transient_retries": self.max_transient_retries,
            "base_backoff_seconds": self.base_backoff_seconds,
            "max_retry_wait_seconds": self.max_retry_wait_seconds,
            "max_total_backoff_seconds": self.max_total_backoff_seconds,
            "max_total_tokens": self.max_total_tokens,
            "max_output_tokens": self.max_output_tokens,
        }

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.persisted_values(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CallReservation:
    attempt_id: str
    sequence_no: int
    logical_call_id: str


class CallLedger(Protocol):
    async def ensure(self, policy: CallPolicy) -> None: ...

    async def reconcile_incomplete(self) -> int: ...

    async def reserve(
        self,
        *,
        logical_call_id: str,
        role: str,
        identity: CallIdentity,
        retry_index: int,
    ) -> CallReservation: ...

    async def mark_started(self, attempt_id: str) -> None: ...

    async def mark_succeeded(self, attempt_id: str, usage: CallUsage | None) -> None: ...

    async def mark_failed(self, attempt_id: str, code: str, *, unknown: bool) -> None: ...

    async def schedule_retry(self, attempt_id: str, kind: str, delay: float) -> None: ...

    async def snapshot(self) -> dict[str, int | float | bool | str | None]: ...


def _mapping_value(value: object, name: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def extract_token_usage(response: object) -> tuple[int, int, int] | None:
    """Extract only complete, provider-reported token counters; never estimate usage."""

    raw = _mapping_value(response, "raw")
    if raw is not None:
        response = raw
    usage = _mapping_value(response, "usage_metadata")
    if usage is not None:
        input_tokens = _mapping_value(usage, "input_tokens")
        output_tokens = _mapping_value(usage, "output_tokens")
        total_tokens = _mapping_value(usage, "total_tokens")
    else:
        metadata = _mapping_value(response, "response_metadata")
        usage = _mapping_value(metadata, "token_usage") if metadata is not None else None
        input_tokens = _mapping_value(usage, "prompt_tokens")
        output_tokens = _mapping_value(usage, "completion_tokens")
        total_tokens = _mapping_value(usage, "total_tokens")
    counters = (input_tokens, output_tokens, total_tokens)
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in counters):
        return None
    return int(input_tokens), int(output_tokens), int(total_tokens)


def _header(response: object, name: str) -> str | None:
    headers = _mapping_value(response, "headers")
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if str(key).casefold() == name.casefold():
                return str(value)
    return None


def parse_retry_after(value: object | None, *, now: datetime | None = None) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return max(0.0, float(value))
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            reference = now or datetime.now(UTC)
            return max(0.0, (retry_at - reference).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _provider_status(error: BaseException) -> object | None:
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None)
    return _mapping_value(response, "status_code") if status is None else status


def _provider_retry_after(error: BaseException) -> float | None:
    response = getattr(error, "response", None)
    retry_after = parse_retry_after(_header(response, "retry-after"))
    if retry_after is None:
        retry_after = parse_retry_after(getattr(error, "retry_after", None))
    return retry_after


def classify_rate_limit(error: BaseException) -> RateLimitError | None:
    if isinstance(error, RateLimitError):
        return error
    status = _provider_status(error)
    message = str(error)
    body = getattr(error, "body", None)
    body_error = _mapping_value(body, "error")
    codes = (
        getattr(error, "code", None),
        _mapping_value(body, "code"),
        _mapping_value(body_error, "code"),
        _mapping_value(body_error, "type"),
    )
    lowered = " ".join([message, *(str(code) for code in codes if code)]).casefold()
    rate_limited = str(status) == "429" or "rate limit" in lowered or "ratelimit" in lowered
    quota_terms = (
        "insufficient_quota",
        "daily quota",
        "quota exceeded",
        "exceeded your current quota",
        "billing quota",
    )
    daily_quota = any(term in lowered for term in quota_terms)
    if not rate_limited and not daily_quota:
        return None
    return RateLimitError(
        "model provider daily quota exhausted" if daily_quota else "model provider rate limit",
        retry_after_seconds=_provider_retry_after(error),
        daily_quota=daily_quota,
    )


def classify_transient_provider_error(error: BaseException) -> TransientProviderError | None:
    if isinstance(error, TransientProviderError):
        return error
    status = _provider_status(error)
    lowered = str(error).casefold()
    retryable_status = str(status) in {"408", "425", "500", "502", "503", "504"}
    connection_error = isinstance(error, (ConnectionError, TimeoutError, OSError))
    connection_message = any(
        term in lowered
        for term in (
            "connection reset",
            "connection aborted",
            "connection refused",
            "connection closed",
            "timed out",
            "timeout",
        )
    )
    outage_message = any(
        term in lowered
        for term in (
            "temporarily unavailable",
            "temporary unavailable",
            "high demand",
            "service unavailable",
            "status': 'unavailable",
            'status": "unavailable',
        )
    )
    if (
        not retryable_status
        and not connection_error
        and not connection_message
        and not outage_message
    ):
        return None
    response_unknown = connection_error or connection_message
    code = (
        "model_provider_response_unknown"
        if response_unknown
        else "model_provider_transient_failure"
    )
    message = (
        "model provider response is unknown"
        if response_unknown
        else "model provider temporarily unavailable"
    )
    return TransientProviderError(
        message,
        retry_after_seconds=_provider_retry_after(error),
        response_unknown=response_unknown,
        code=code,
    )


def classify_provider_error(error: BaseException) -> ProviderRequestError:
    rate_limit = classify_rate_limit(error)
    if rate_limit is not None:
        return rate_limit
    transient = classify_transient_provider_error(error)
    if transient is not None:
        return transient
    status = _provider_status(error)
    if status is not None and str(status).startswith("4"):
        return NonRetryableProviderError()
    if isinstance(error, InvalidProviderResponse):
        return NonRetryableProviderError(code=error.code)
    if isinstance(error, ProviderRequestError):
        return error
    return NonRetryableProviderError(code="model_provider_non_retryable_failure")


def _empty_snapshot(policy: CallPolicy) -> dict[str, int | float | bool | str | None]:
    return {
        "call_control_version": policy.version,
        "model_calls": 0,
        "reserved_calls": 0,
        "started_calls": 0,
        "successful_model_calls": 0,
        "failed_calls": 0,
        "unknown_calls": 0,
        "pending_reserved_calls": 0,
        "pending_started_calls": 0,
        "rate_limit_retries": 0,
        "transient_retries": 0,
        "fallback_calls": 0,
        "max_model_calls": policy.max_calls,
        "backoff_seconds": 0.0,
        "max_total_backoff_seconds": policy.max_total_backoff_seconds,
        "observed_input_tokens": 0,
        "observed_output_tokens": 0,
        "observed_total_tokens": 0,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "token_usage_complete": False,
        "max_total_tokens": policy.max_total_tokens,
        "max_output_tokens": policy.max_output_tokens,
    }


class InMemoryCallLedger:
    """Evaluation/test ledger with the same state transitions as the database ledger."""

    def __init__(
        self,
        policy: CallPolicy,
        *,
        initial_snapshot: Mapping[str, Any] | None = None,
        state_hook: StateHook | None = None,
    ) -> None:
        self.policy = policy
        self.state_hook = state_hook
        self._lock = asyncio.Lock()
        self._attempts: dict[str, dict[str, Any]] = {}
        self._values = _empty_snapshot(policy)
        if initial_snapshot:
            self._restore(initial_snapshot)

    def _restore(self, snapshot: Mapping[str, Any]) -> None:
        reserved = int(snapshot.get("reserved_calls", snapshot.get("model_calls", 0)) or 0)
        succeeded = int(snapshot.get("successful_model_calls", 0) or 0)
        failed = int(snapshot.get("failed_calls", 0) or 0)
        unknown = int(snapshot.get("unknown_calls", 0) or 0)
        pending_reserved = int(snapshot.get("pending_reserved_calls", 0) or 0)
        pending_started = int(snapshot.get("pending_started_calls", 0) or 0)
        accounted = succeeded + failed + unknown + pending_reserved + pending_started
        if accounted < reserved:
            # Schema-v3 evaluation records lacked outcome buckets. Failed/in-flight calls
            # are conservatively restored as unknown instead of becoming free attempts.
            unknown += reserved - accounted
        self._values.update(
            {
                "model_calls": reserved,
                "reserved_calls": reserved,
                "started_calls": int(snapshot.get("started_calls", reserved) or 0),
                "successful_model_calls": succeeded,
                "failed_calls": failed,
                "unknown_calls": unknown,
                "pending_reserved_calls": pending_reserved,
                "pending_started_calls": pending_started,
                "rate_limit_retries": int(snapshot.get("rate_limit_retries", 0) or 0),
                "transient_retries": int(snapshot.get("transient_retries", 0) or 0),
                "fallback_calls": int(snapshot.get("fallback_calls", 0) or 0),
                "backoff_seconds": float(snapshot.get("backoff_seconds", 0.0) or 0.0),
                "observed_input_tokens": int(snapshot.get("observed_input_tokens", 0) or 0),
                "observed_output_tokens": int(snapshot.get("observed_output_tokens", 0) or 0),
                "observed_total_tokens": int(snapshot.get("observed_total_tokens", 0) or 0),
            }
        )
        complete = bool(snapshot.get("token_usage_complete", False))
        self._values["_usage_complete"] = complete
        self._refresh_exact_usage()

    async def _changed(self) -> None:
        if self.state_hook is not None:
            await self.state_hook(await self.snapshot())

    def _refresh_exact_usage(self) -> None:
        succeeded = int(self._values["successful_model_calls"] or 0)
        complete = succeeded > 0 and bool(self._values.get("_usage_complete", True))
        self._values["token_usage_complete"] = complete
        for public, observed in (
            ("input_tokens", "observed_input_tokens"),
            ("output_tokens", "observed_output_tokens"),
            ("total_tokens", "observed_total_tokens"),
        ):
            self._values[public] = self._values[observed] if complete else None

    async def ensure(self, policy: CallPolicy) -> None:
        if policy != self.policy:
            raise CallPolicyMismatch

    async def reconcile_incomplete(self) -> int:
        async with self._lock:
            pending = int(self._values["pending_started_calls"] or 0)
            if pending:
                self._values["pending_started_calls"] = 0
                self._values["unknown_calls"] = int(self._values["unknown_calls"] or 0) + pending
            for attempt in self._attempts.values():
                if attempt["status"] == CallStatus.STARTED:
                    attempt["status"] = CallStatus.UNKNOWN
        if pending:
            await self._changed()
        return pending

    async def reserve(
        self,
        *,
        logical_call_id: str,
        role: str,
        identity: CallIdentity,
        retry_index: int,
    ) -> CallReservation:
        async with self._lock:
            used = int(self._values["reserved_calls"] or 0)
            if used >= self.policy.max_calls:
                raise CallBudgetExceeded(used, self.policy.max_calls)
            observed = int(self._values["observed_total_tokens"] or 0)
            usage_complete = bool(self._values.get("_usage_complete", True))
            if (
                self.policy.max_total_tokens is not None
                and usage_complete
                and observed >= self.policy.max_total_tokens
            ):
                raise TokenBudgetExceeded(observed, self.policy.max_total_tokens)
            attempt_id = str(uuid.uuid4())
            sequence = used + 1
            self._attempts[attempt_id] = {
                "status": CallStatus.RESERVED,
                "logical_call_id": logical_call_id,
                "role": role,
                "identity": identity,
                "retry_index": retry_index,
            }
            self._values["model_calls"] = sequence
            self._values["reserved_calls"] = sequence
            self._values["pending_reserved_calls"] = (
                int(self._values["pending_reserved_calls"] or 0) + 1
            )
            if identity.is_fallback:
                self._values["fallback_calls"] = int(self._values["fallback_calls"] or 0) + 1
        await self._changed()
        return CallReservation(attempt_id, sequence, logical_call_id)

    async def mark_started(self, attempt_id: str) -> None:
        async with self._lock:
            attempt = self._attempts[attempt_id]
            if attempt["status"] != CallStatus.RESERVED:
                raise CallLedgerStateConflict(attempt_id, CallStatus.RESERVED)
            attempt["status"] = CallStatus.STARTED
            self._values["pending_reserved_calls"] = (
                int(self._values["pending_reserved_calls"] or 0) - 1
            )
            self._values["pending_started_calls"] = (
                int(self._values["pending_started_calls"] or 0) + 1
            )
            self._values["started_calls"] = int(self._values["started_calls"] or 0) + 1
        await self._changed()

    async def mark_succeeded(self, attempt_id: str, usage: CallUsage | None) -> None:
        async with self._lock:
            attempt = self._attempts[attempt_id]
            if attempt["status"] != CallStatus.STARTED:
                raise CallLedgerStateConflict(attempt_id, CallStatus.STARTED)
            attempt["status"] = CallStatus.SUCCEEDED
            self._values["pending_started_calls"] = (
                int(self._values["pending_started_calls"] or 0) - 1
            )
            self._values["successful_model_calls"] = (
                int(self._values["successful_model_calls"] or 0) + 1
            )
            if usage is None:
                self._values["_usage_complete"] = False
            else:
                self._values["observed_input_tokens"] = (
                    int(self._values["observed_input_tokens"] or 0) + usage.input_tokens
                )
                self._values["observed_output_tokens"] = (
                    int(self._values["observed_output_tokens"] or 0) + usage.output_tokens
                )
                self._values["observed_total_tokens"] = (
                    int(self._values["observed_total_tokens"] or 0) + usage.total_tokens
                )
            self._refresh_exact_usage()
        await self._changed()

    async def mark_failed(self, attempt_id: str, code: str, *, unknown: bool) -> None:
        async with self._lock:
            attempt = self._attempts[attempt_id]
            if attempt["status"] != CallStatus.STARTED:
                raise CallLedgerStateConflict(attempt_id, CallStatus.STARTED)
            attempt["status"] = CallStatus.UNKNOWN if unknown else CallStatus.FAILED
            attempt["error_code"] = code
            self._values["pending_started_calls"] = (
                int(self._values["pending_started_calls"] or 0) - 1
            )
            key = "unknown_calls" if unknown else "failed_calls"
            self._values[key] = int(self._values[key] or 0) + 1
            self._values["_usage_complete"] = False
            self._refresh_exact_usage()
        await self._changed()

    async def schedule_retry(self, attempt_id: str, kind: str, delay: float) -> None:
        del attempt_id
        async with self._lock:
            current = float(self._values["backoff_seconds"] or 0.0)
            if current + delay > self.policy.max_total_backoff_seconds:
                raise BackoffBudgetExceeded(
                    delay, max(0.0, self.policy.max_total_backoff_seconds - current)
                )
            self._values["backoff_seconds"] = current + delay
            key = "rate_limit_retries" if kind == "rate_limit" else "transient_retries"
            self._values[key] = int(self._values[key] or 0) + 1
        await self._changed()

    async def snapshot(self) -> dict[str, int | float | bool | str | None]:
        async with self._lock:
            return {key: value for key, value in self._values.items() if not key.startswith("_")}

    def snapshot_now(self) -> dict[str, int | float | bool | str | None]:
        return {key: value for key, value in self._values.items() if not key.startswith("_")}


class ControlledModelCaller:
    """Provider-neutral call boundary with durable reservation and bounded retry rules."""

    def __init__(
        self,
        ledger: CallLedger,
        policy: CallPolicy,
        *,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        self.ledger = ledger
        self.policy = policy
        self.sleeper = sleeper

    async def call(
        self,
        invocation: Callable[..., Awaitable[_T]],
        *args: object,
        call_identity: CallIdentity | None = None,
        call_role: str = "unspecified",
        response_validator: ResponseValidator | None = None,
        usage_extractor: UsageExtractor = extract_token_usage,
        **kwargs: object,
    ) -> _T:
        identity = call_identity or CallIdentity(
            provider="unspecified",
            model="unspecified",
            adapter_version=DEFAULT_ADAPTER_VERSION,
            request_schema_version="unspecified-v1",
        )
        logical_call_id = str(uuid.uuid4())
        rate_limit_retries = 0
        transient_retries = 0
        retry_index = 0
        while True:
            reservation = await self.ledger.reserve(
                logical_call_id=logical_call_id,
                role=call_role,
                identity=identity,
                retry_index=retry_index,
            )
            # This commit precedes the transport call. A crash after it is conservatively
            # reconciled to UNKNOWN even if the process died just before bytes were sent.
            await self.ledger.mark_started(reservation.attempt_id)
            try:
                response = await asyncio.wait_for(
                    invocation(*args, **kwargs),
                    timeout=self.policy.request_timeout_seconds,
                )
                if response_validator is not None:
                    response_validator(response)
            except Exception as error:
                normalized = classify_provider_error(error)
                await self.ledger.mark_failed(
                    reservation.attempt_id,
                    normalized.code,
                    unknown=normalized.response_unknown,
                )
                retry_count = (
                    rate_limit_retries
                    if normalized.retry_kind == "rate_limit"
                    else transient_retries
                )
                retry_limit = (
                    self.policy.max_rate_limit_retries
                    if normalized.retry_kind == "rate_limit"
                    else self.policy.max_transient_retries
                )
                if not normalized.retryable or retry_count >= retry_limit:
                    raise normalized from None
                delay = normalized.retry_after_seconds
                if delay is None:
                    delay = self.policy.base_backoff_seconds * (2**retry_index)
                if delay > self.policy.max_retry_wait_seconds:
                    raise RetryWaitLimitExceeded(
                        delay, self.policy.max_retry_wait_seconds
                    ) from None
                await self.ledger.schedule_retry(
                    reservation.attempt_id,
                    normalized.retry_kind,
                    delay,
                )
                if normalized.retry_kind == "rate_limit":
                    rate_limit_retries += 1
                else:
                    transient_retries += 1
                retry_index += 1
                await self.sleeper(delay)
                continue
            usage_values = usage_extractor(response)
            usage = CallUsage(*usage_values) if usage_values is not None else None
            await self.ledger.mark_succeeded(reservation.attempt_id, usage)
            return response


class ModelCallBudget:
    """Compatibility facade used by evaluations and non-database callers.

    The historical API remains importable, while all counting, classification, timeout,
    Retry-After, and backoff decisions now run through :class:`ControlledModelCaller`.
    """

    def __init__(
        self,
        max_calls: int,
        *,
        max_rate_limit_retries: int = 2,
        max_transient_retries: int = 2,
        base_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 30.0,
        max_total_backoff_seconds: float = 60.0,
        request_timeout_seconds: float = 90.0,
        max_total_tokens: int | None = None,
        max_output_tokens: int = 4_096,
        sleeper: Sleeper | None = None,
        initial_snapshot: Mapping[str, Any] | None = None,
        state_hook: StateHook | None = None,
        policy_version: str = EVALUATION_BUDGET_VERSION,
    ) -> None:
        self.policy = CallPolicy(
            max_calls=max_calls,
            request_timeout_seconds=request_timeout_seconds,
            max_rate_limit_retries=max_rate_limit_retries,
            max_transient_retries=max_transient_retries,
            base_backoff_seconds=base_backoff_seconds,
            max_retry_wait_seconds=max_backoff_seconds,
            max_total_backoff_seconds=max_total_backoff_seconds,
            max_total_tokens=max_total_tokens,
            max_output_tokens=max_output_tokens,
            version=policy_version,
        )
        self._ledger = InMemoryCallLedger(
            self.policy,
            initial_snapshot=initial_snapshot,
            state_hook=state_hook,
        )
        self._caller = ControlledModelCaller(
            self._ledger,
            self.policy,
            sleeper=sleeper or asyncio.sleep,
        )

    @property
    def max_calls(self) -> int:
        return self.policy.max_calls

    @property
    def calls_started(self) -> int:
        return int(self.snapshot()["model_calls"] or 0)

    @property
    def calls_succeeded(self) -> int:
        return int(self.snapshot()["successful_model_calls"] or 0)

    @property
    def rate_limit_retries(self) -> int:
        return int(self.snapshot()["rate_limit_retries"] or 0)

    @property
    def transient_retries(self) -> int:
        return int(self.snapshot()["transient_retries"] or 0)

    async def reconcile_incomplete(self) -> int:
        return await self._ledger.reconcile_incomplete()

    async def call(
        self,
        invocation: Callable[..., Awaitable[_T]],
        *args: object,
        call_identity: CallIdentity | None = None,
        call_role: str = "unspecified",
        response_validator: ResponseValidator | None = None,
        usage_extractor: UsageExtractor = extract_token_usage,
        **kwargs: object,
    ) -> _T:
        return await self._caller.call(
            invocation,
            *args,
            call_identity=call_identity,
            call_role=call_role,
            response_validator=response_validator,
            usage_extractor=usage_extractor,
            **kwargs,
        )

    def snapshot(self) -> dict[str, int | float | bool | str | None]:
        return self._ledger.snapshot_now()


__all__ = [
    "CALL_CONTROL_VERSION",
    "EVALUATION_BUDGET_VERSION",
    "BackoffBudgetExceeded",
    "CallBudgetExceeded",
    "CallIdentity",
    "CallLedger",
    "CallLedgerStateConflict",
    "CallPolicy",
    "CallPolicyMismatch",
    "CallReservation",
    "CallStatus",
    "CallUsage",
    "ControlledModelCaller",
    "InMemoryCallLedger",
    "InvalidProviderResponse",
    "ModelCallBudget",
    "ModelCallControlError",
    "NonRetryableProviderError",
    "ProviderRequestError",
    "RateLimitError",
    "RetryWaitLimitExceeded",
    "TokenBudgetExceeded",
    "TransientProviderError",
    "classify_provider_error",
    "classify_rate_limit",
    "classify_transient_provider_error",
    "extract_token_usage",
    "parse_retry_after",
]
