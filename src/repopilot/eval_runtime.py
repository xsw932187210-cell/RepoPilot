"""Resumable, provider-neutral execution primitives for real evaluations.

The runtime deliberately knows nothing about repositories or sandboxes.  A real-task
harness supplies an async callback and receives a fresh :class:`ModelCallBudget` for
each case/mode pair.  This keeps persistence and quota handling testable without a
network, while allowing the harness to choose Docker, a remote worker, or another
isolated evaluator.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, TypeVar

from repopilot.security import redact_secrets

SCHEMA_VERSION = 3
_T = TypeVar("_T")
_DROPPED_EVIDENCE_KEYS = {
    "acceptance_command",
    "command",
    "diff",
    "junit_xml",
    "logs",
    "outcomes",
    "patch",
    "raw_junit",
    "raw_output",
    "stderr",
    "stdout",
    "visible_command",
}
_IDENTIFIER_LIST_COUNTS = {
    "baseline_identity_drift": "baseline_identity_drift_count",
    "extra_tests": "extra_count",
    "missing_tests": "missing_count",
    "regressions": "regression_count",
}


class ResumeMismatchError(ValueError):
    """Raised when a record directory belongs to a different experiment."""


class CallBudgetExceeded(RuntimeError):
    """Raised before an invocation which would exceed the configured call cap."""


class RateLimitError(RuntimeError):
    """Normalized provider rate-limit signal understood by the evaluation runtime."""

    def __init__(
        self,
        message: str = "model provider rate limit",
        *,
        retry_after_seconds: float | None = None,
        daily_quota: bool = False,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.daily_quota = daily_quota


class TransientProviderError(RuntimeError):
    """Normalized retryable provider outage (for example HTTP 503)."""

    def __init__(
        self,
        message: str = "model provider temporarily unavailable",
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def dataset_sha256(data: str | bytes) -> str:
    """Return the full dataset digest used in experiment identity."""

    encoded = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_value(value: object) -> object:
    """Copy a value through JSON so mutable caller-owned config cannot drift."""

    return json.loads(_canonical_json(value))


def _sanitize_evidence(value: object) -> object:
    """Return checkpoint-safe evidence without patches, logs, or test identities."""

    if isinstance(value, Mapping):
        safe: dict[str, object] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in _DROPPED_EVIDENCE_KEYS:
                continue
            count_key = _IDENTIFIER_LIST_COUNTS.get(key)
            if count_key is not None:
                if isinstance(item, (list, tuple, set, frozenset)):
                    safe[count_key] = len(item)
                elif isinstance(item, int) and not isinstance(item, bool):
                    safe[count_key] = item
                else:
                    safe[count_key] = 0
                continue
            safe[key] = _sanitize_evidence(item)
        return safe
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize_evidence(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    """Every field that can affect comparability of an evaluation run."""

    dataset_sha256: str
    provider: str
    model: str
    temperature: float
    max_model_calls: int
    modes: tuple[str, ...] = ("workflow",)
    context_budget: Mapping[str, Any] = field(default_factory=dict)
    test_evaluator: str = "repository-tests-v1"
    evaluator_config: Mapping[str, Any] = field(default_factory=dict)
    _context_json: str = field(init=False, repr=False, compare=False)
    _evaluator_json: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.dataset_sha256):
            raise ValueError("dataset_sha256 must be a full lowercase SHA-256 digest")
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("provider and model must be named")
        if self.max_model_calls < 1:
            raise ValueError("max_model_calls must be positive")
        object.__setattr__(self, "modes", tuple(self.modes))
        if not self.modes or len(set(self.modes)) != len(self.modes):
            raise ValueError("modes must be non-empty and unique")
        if any(not mode.strip() for mode in self.modes):
            raise ValueError("mode names must be non-empty")
        # Validate serializability now rather than halfway through a paid run.
        object.__setattr__(self, "_context_json", _canonical_json(self.context_budget))
        object.__setattr__(self, "_evaluator_json", _canonical_json(self.evaluator_config))

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset_sha256": self.dataset_sha256,
            "provider": self.provider,
            "model": self.model,
            "temperature": self.temperature,
            "max_model_calls": self.max_model_calls,
            "modes": list(self.modes),
            "context_budget": json.loads(self._context_json),
            "test_evaluator": self.test_evaluator,
            "evaluator_config": json.loads(self._evaluator_json),
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical_json(self.as_dict()).encode("utf-8")).hexdigest()


def _mapping_value(value: object, name: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def extract_token_usage(response: object) -> tuple[int, int, int] | None:
    """Extract exact token counts from common LangChain/OpenAI response shapes.

    No estimate is attempted.  If any counter is absent, the result is ``None``.
    """

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


def _retry_after_seconds(value: object | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return max(0.0, float(value))
    if not isinstance(value, str) or not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def classify_rate_limit(error: BaseException) -> RateLimitError | None:
    """Normalize 429-style SDK exceptions without depending on one SDK version."""

    if isinstance(error, RateLimitError):
        return error
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None)
    if status is None and response is not None:
        status = _mapping_value(response, "status_code")
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
    retry_after = _retry_after_seconds(_header(response, "retry-after"))
    if retry_after is None:
        retry_after = _retry_after_seconds(getattr(error, "retry_after", None))
    return RateLimitError(
        "model provider daily quota exhausted" if daily_quota else "model provider rate limit",
        retry_after_seconds=retry_after,
        daily_quota=daily_quota,
    )


def classify_transient_provider_error(error: BaseException) -> TransientProviderError | None:
    """Normalize bounded-retry transport and 5xx failures without an SDK dependency."""

    if isinstance(error, TransientProviderError):
        return error
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None)
    if status is None and response is not None:
        status = _mapping_value(response, "status_code")
    lowered = str(error).casefold()
    retryable_status = str(status) in {"408", "500", "502", "503", "504"}
    retryable_message = any(
        term in lowered
        for term in (
            "temporarily unavailable",
            "temporary unavailable",
            "high demand",
            "service unavailable",
            "status': 'unavailable",
            'status": "unavailable',
            "connection reset",
            "connection aborted",
        )
    )
    if not retryable_status and not retryable_message:
        return None
    retry_after = _retry_after_seconds(_header(response, "retry-after"))
    if retry_after is None:
        retry_after = _retry_after_seconds(getattr(error, "retry_after", None))
    return TransientProviderError(retry_after_seconds=retry_after)


class ModelCallBudget:
    """Enforce an exact invocation cap and collect provider-reported usage."""

    def __init__(
        self,
        max_calls: int,
        *,
        max_rate_limit_retries: int = 2,
        max_transient_retries: int = 2,
        base_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 30.0,
        sleeper: Sleeper | None = None,
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        if max_rate_limit_retries < 0:
            raise ValueError("max_rate_limit_retries cannot be negative")
        if max_transient_retries < 0:
            raise ValueError("max_transient_retries cannot be negative")
        if base_backoff_seconds < 0 or max_backoff_seconds < 0:
            raise ValueError("backoff durations cannot be negative")
        self.max_calls = max_calls
        self.max_rate_limit_retries = max_rate_limit_retries
        self.max_transient_retries = max_transient_retries
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.sleeper = sleeper or asyncio.sleep
        self.calls_started = 0
        self.calls_succeeded = 0
        self.rate_limit_retries = 0
        self.transient_retries = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_tokens = 0
        self._token_usage_complete = True
        self._lock = asyncio.Lock()

    async def call(
        self,
        invocation: Callable[..., Awaitable[_T]],
        *args: object,
        usage_extractor: Callable[[object], tuple[int, int, int] | None] = extract_token_usage,
        **kwargs: object,
    ) -> _T:
        retry_index = 0
        while True:
            async with self._lock:
                if self.calls_started >= self.max_calls:
                    raise CallBudgetExceeded(
                        f"model call budget exhausted ({self.calls_started}/{self.max_calls})"
                    )
                self.calls_started += 1
            try:
                response = await invocation(*args, **kwargs)
                break
            except Exception as error:
                # Failed requests make aggregate token totals incomplete. Exact usage from
                # preceding successful calls remains available as the observed counters.
                self._token_usage_complete = False
                rate_limit = classify_rate_limit(error)
                transient = (
                    None if rate_limit is not None else classify_transient_provider_error(error)
                )
                normalized = rate_limit or transient
                if normalized is None:
                    raise
                retry_after = normalized.retry_after_seconds
                delay = (
                    retry_after
                    if retry_after is not None
                    else self.base_backoff_seconds * (2**retry_index)
                )
                retry_limit = (
                    self.max_rate_limit_retries
                    if rate_limit is not None
                    else self.max_transient_retries
                )
                daily_quota = bool(rate_limit and rate_limit.daily_quota)
                may_retry = (
                    not daily_quota
                    and retry_index < retry_limit
                    and delay <= self.max_backoff_seconds
                    and self.calls_started < self.max_calls
                )
                if not may_retry:
                    raise normalized from error
                if rate_limit is not None:
                    self.rate_limit_retries += 1
                else:
                    self.transient_retries += 1
                retry_index += 1
                await self.sleeper(delay)
        self.calls_succeeded += 1
        usage = usage_extractor(response)
        if usage is None:
            self._token_usage_complete = False
        else:
            self._input_tokens += usage[0]
            self._output_tokens += usage[1]
            self._total_tokens += usage[2]
        return response

    def snapshot(self) -> dict[str, int | bool | None]:
        complete = self.calls_started > 0 and self._token_usage_complete
        return {
            "model_calls": self.calls_started,
            "successful_model_calls": self.calls_succeeded,
            "rate_limit_retries": self.rate_limit_retries,
            "transient_retries": self.transient_retries,
            "max_model_calls": self.max_calls,
            "observed_input_tokens": self._input_tokens,
            "observed_output_tokens": self._output_tokens,
            "observed_total_tokens": self._total_tokens,
            "input_tokens": self._input_tokens if complete else None,
            "output_tokens": self._output_tokens if complete else None,
            "total_tokens": self._total_tokens if complete else None,
            "token_usage_complete": complete,
        }


CaseExecutor = Callable[[Mapping[str, Any], str, ModelCallBudget], Awaitable[Mapping[str, Any]]]
Sleeper = Callable[[float], Awaitable[None]]


def _safe_error(error: BaseException) -> dict[str, str]:
    return {"type": type(error).__name__, "message": redact_secrets(str(error))[:2_000]}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class EvaluationRuntime:
    """Run and atomically checkpoint independent evaluation case/mode pairs."""

    def __init__(
        self,
        config: EvaluationConfig,
        record_dir: Path,
        *,
        max_rate_limit_retries: int = 2,
        max_transient_retries: int = 2,
        base_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 30.0,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        if max_rate_limit_retries < 0:
            raise ValueError("max_rate_limit_retries cannot be negative")
        if max_transient_retries < 0:
            raise ValueError("max_transient_retries cannot be negative")
        if base_backoff_seconds < 0 or max_backoff_seconds < 0:
            raise ValueError("backoff durations cannot be negative")
        self.config = config
        self.record_dir = record_dir
        self.max_rate_limit_retries = max_rate_limit_retries
        self.max_transient_retries = max_transient_retries
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.sleeper = sleeper

    @property
    def manifest_path(self) -> Path:
        return self.record_dir / "run.json"

    @property
    def runtime_policy(self) -> dict[str, int | float]:
        return {
            "max_rate_limit_retries": self.max_rate_limit_retries,
            "max_transient_retries": self.max_transient_retries,
            "base_backoff_seconds": self.base_backoff_seconds,
            "max_backoff_seconds": self.max_backoff_seconds,
        }

    @property
    def fingerprint(self) -> str:
        identity = {"config": self.config.as_dict(), "runtime_policy": self.runtime_policy}
        return hashlib.sha256(_canonical_json(identity).encode()).hexdigest()

    @property
    def cases_dir(self) -> Path:
        return self.record_dir / "cases"

    def _record_path(self, case_id: str, mode: str) -> Path:
        key = hashlib.sha256(f"{case_id}\0{mode}".encode()).hexdigest()
        return self.cases_dir / f"{key}.json"

    def _atomic_write(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = f"{json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)}\n"
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _prepare_manifest(self) -> None:
        expected = {
            "schema_version": SCHEMA_VERSION,
            "fingerprint": self.fingerprint,
            "config": self.config.as_dict(),
            "runtime_policy": self.runtime_policy,
        }
        if self.manifest_path.exists():
            existing = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if existing != expected:
                raise ResumeMismatchError(
                    "evaluation record directory fingerprint/config does not match this run"
                )
            return
        if self.record_dir.exists() and any(self.record_dir.iterdir()):
            raise ResumeMismatchError("non-empty evaluation record directory has no run manifest")
        self._atomic_write(self.manifest_path, expected)

    def _load_record(self, case_id: str, mode: str) -> dict[str, Any] | None:
        path = self._record_path(case_id, mode)
        if not path.exists():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        identity = (record.get("fingerprint"), record.get("case_id"), record.get("mode"))
        if identity != (self.fingerprint, case_id, mode):
            raise ResumeMismatchError(f"checkpoint identity mismatch for {case_id!r}/{mode!r}")
        return record

    def _write_record(self, record: Mapping[str, Any]) -> None:
        self._atomic_write(self._record_path(str(record["case_id"]), str(record["mode"])), record)

    @staticmethod
    def _case_id(case: Mapping[str, Any]) -> str:
        value = case.get("id", case.get("name"))
        if not isinstance(value, str) or not value.strip():
            raise ValueError("each evaluation case requires a non-empty string id or name")
        return value

    async def _execute_one(
        self,
        case: Mapping[str, Any],
        case_id: str,
        mode: str,
        execute: CaseExecutor,
    ) -> tuple[dict[str, Any], bool]:
        budget = ModelCallBudget(
            self.config.max_model_calls,
            max_rate_limit_retries=self.max_rate_limit_retries,
            max_transient_retries=self.max_transient_retries,
            base_backoff_seconds=self.base_backoff_seconds,
            max_backoff_seconds=self.max_backoff_seconds,
            sleeper=self.sleeper,
        )
        started_at = _utc_now()
        started_clock = time.perf_counter()
        attempts = 0
        running = {
            "schema_version": SCHEMA_VERSION,
            "fingerprint": self.fingerprint,
            "case_id": case_id,
            "mode": mode,
            "status": "running",
            "started_at": started_at,
            "finished_at": None,
            "duration_ms": None,
            "attempts": 0,
            "result": None,
            "error": None,
            "usage": budget.snapshot(),
        }
        self._write_record(running)
        attempts += 1
        try:
            result = await execute(case, mode, budget)
            if not isinstance(result, Mapping):
                raise TypeError("evaluation executor must return a mapping")
            record = {
                **running,
                "status": "completed",
                "finished_at": _utc_now(),
                "duration_ms": int((time.perf_counter() - started_clock) * 1_000),
                "attempts": attempts,
                "result": _json_value(_sanitize_evidence(result)),
                "usage": budget.snapshot(),
            }
            self._write_record(record)
            return record, False
        except Exception as error:
            normalized = classify_rate_limit(error)
            if normalized is not None:
                record = {
                    **running,
                    "status": "pending_quota",
                    "finished_at": _utc_now(),
                    "duration_ms": int((time.perf_counter() - started_clock) * 1_000),
                    "attempts": attempts,
                    "result": None,
                    "error": _safe_error(normalized),
                    "usage": budget.snapshot(),
                }
                self._write_record(record)
                return record, True
            record = {
                **running,
                "status": "failed",
                "finished_at": _utc_now(),
                "duration_ms": int((time.perf_counter() - started_clock) * 1_000),
                "attempts": attempts,
                "result": None,
                "error": _safe_error(error),
                "usage": budget.snapshot(),
            }
            self._write_record(record)
            return record, False

    def _pending_record(self, case_id: str, mode: str) -> dict[str, Any]:
        now = _utc_now()
        record = {
            "schema_version": SCHEMA_VERSION,
            "fingerprint": self.fingerprint,
            "case_id": case_id,
            "mode": mode,
            "status": "pending_quota",
            "started_at": None,
            "finished_at": now,
            "duration_ms": None,
            "attempts": 0,
            "result": None,
            "error": {"type": "RateLimitError", "message": "run stopped by provider quota"},
            "usage": ModelCallBudget(self.config.max_model_calls).snapshot(),
        }
        self._write_record(record)
        return record

    async def run(
        self,
        cases: Iterable[Mapping[str, Any]],
        execute: CaseExecutor,
    ) -> dict[str, Any]:
        """Resume a run, isolating failures and stopping cleanly on unresolved quota."""

        if not callable(execute):
            raise TypeError("execute must be an async callable")
        case_list = list(cases)
        case_ids = [self._case_id(case) for case in case_list]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("evaluation case ids/names must be unique")
        self._prepare_manifest()

        records: list[dict[str, Any]] = []
        quota_stopped = False
        for case, case_id in zip(case_list, case_ids, strict=True):
            for mode in self.config.modes:
                existing = self._load_record(case_id, mode)
                if existing and existing.get("status") in {"completed", "failed"}:
                    records.append(existing)
                    continue
                if quota_stopped:
                    record = self._pending_record(case_id, mode)
                    records.append(record)
                    continue
                record, quota_stopped = await self._execute_one(case, case_id, mode, execute)
                records.append(record)

        return self.report(records)

    def report(self, records: list[Mapping[str, Any]]) -> dict[str, Any]:
        status_counts = {
            status: sum(record.get("status") == status for record in records)
            for status in ("completed", "failed", "pending_quota")
        }
        per_mode: dict[str, dict[str, int]] = {}
        for mode in self.config.modes:
            selected = [record for record in records if record.get("mode") == mode]
            per_mode[mode] = {
                "count": len(selected),
                "completed": sum(record.get("status") == "completed" for record in selected),
                "failed": sum(record.get("status") == "failed" for record in selected),
                "pending": sum(record.get("status") == "pending_quota" for record in selected),
                "successful": sum(
                    record.get("status") == "completed"
                    and bool((record.get("result") or {}).get("success"))
                    for record in selected
                ),
                "actual_model_calls": sum(
                    int((record.get("usage") or {}).get("model_calls") or 0) for record in selected
                ),
            }
        return {
            "metadata": {
                "schema_version": SCHEMA_VERSION,
                "fingerprint": self.fingerprint,
                "config": self.config.as_dict(),
                "runtime_policy": self.runtime_policy,
                "record_dir": str(self.record_dir),
            },
            "records": records,
            "summary": {
                "count": len(records),
                **status_counts,
                "pending": status_counts["pending_quota"],
                "per_mode": per_mode,
            },
        }
