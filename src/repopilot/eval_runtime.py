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
from pathlib import Path
from typing import Any

from repopilot.model_calls import (
    EVALUATION_BUDGET_VERSION,
    CallBudgetExceeded,
    ModelCallBudget,
    RateLimitError,
    TransientProviderError,
    classify_rate_limit,
    classify_transient_provider_error,
    extract_token_usage,
)
from repopilot.security import redact_secrets

SCHEMA_VERSION = 4
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
        max_total_backoff_seconds: float = 60.0,
        request_timeout_seconds: float = 90.0,
        max_output_tokens: int = 4_096,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        if max_rate_limit_retries < 0:
            raise ValueError("max_rate_limit_retries cannot be negative")
        if max_transient_retries < 0:
            raise ValueError("max_transient_retries cannot be negative")
        if (
            base_backoff_seconds < 0
            or max_backoff_seconds < 0
            or max_total_backoff_seconds < 0
        ):
            raise ValueError("backoff durations cannot be negative")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        self.config = config
        self.record_dir = record_dir
        self.max_rate_limit_retries = max_rate_limit_retries
        self.max_transient_retries = max_transient_retries
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.max_total_backoff_seconds = max_total_backoff_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.sleeper = sleeper

    @property
    def manifest_path(self) -> Path:
        return self.record_dir / "run.json"

    @property
    def runtime_policy(self) -> dict[str, int | float | str]:
        return {
            "call_control_version": EVALUATION_BUDGET_VERSION,
            "max_rate_limit_retries": self.max_rate_limit_retries,
            "max_transient_retries": self.max_transient_retries,
            "base_backoff_seconds": self.base_backoff_seconds,
            "max_backoff_seconds": self.max_backoff_seconds,
            "max_total_backoff_seconds": self.max_total_backoff_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_output_tokens": self.max_output_tokens,
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
        existing: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        started_at = _utc_now()
        started_clock = time.perf_counter()
        initial_usage = (existing or {}).get("usage")
        attempts = int((existing or {}).get("attempts") or 0) + 1
        running = {
            "schema_version": SCHEMA_VERSION,
            "fingerprint": self.fingerprint,
            "case_id": case_id,
            "mode": mode,
            "status": "running",
            "started_at": started_at,
            "finished_at": None,
            "duration_ms": None,
            "attempts": attempts,
            "result": None,
            "error": None,
            "usage": dict(initial_usage) if isinstance(initial_usage, Mapping) else {},
        }

        async def persist_usage(snapshot: Mapping[str, Any]) -> None:
            running["usage"] = dict(snapshot)
            self._write_record(running)

        budget = ModelCallBudget(
            self.config.max_model_calls,
            max_rate_limit_retries=self.max_rate_limit_retries,
            max_transient_retries=self.max_transient_retries,
            base_backoff_seconds=self.base_backoff_seconds,
            max_backoff_seconds=self.max_backoff_seconds,
            max_total_backoff_seconds=self.max_total_backoff_seconds,
            request_timeout_seconds=self.request_timeout_seconds,
            max_output_tokens=self.max_output_tokens,
            sleeper=self.sleeper,
            initial_snapshot=initial_usage if isinstance(initial_usage, Mapping) else None,
            state_hook=persist_usage,
        )
        await budget.reconcile_incomplete()
        running["usage"] = budget.snapshot()
        self._write_record(running)
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
            "usage": ModelCallBudget(
                self.config.max_model_calls,
                max_rate_limit_retries=self.max_rate_limit_retries,
                max_transient_retries=self.max_transient_retries,
                base_backoff_seconds=self.base_backoff_seconds,
                max_backoff_seconds=self.max_backoff_seconds,
                max_total_backoff_seconds=self.max_total_backoff_seconds,
                request_timeout_seconds=self.request_timeout_seconds,
                max_output_tokens=self.max_output_tokens,
            ).snapshot(),
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
                record, quota_stopped = await self._execute_one(
                    case, case_id, mode, execute, existing
                )
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


__all__ = [
    "SCHEMA_VERSION",
    "CallBudgetExceeded",
    "EvaluationConfig",
    "EvaluationRuntime",
    "ModelCallBudget",
    "RateLimitError",
    "ResumeMismatchError",
    "TransientProviderError",
    "classify_rate_limit",
    "classify_transient_provider_error",
    "dataset_sha256",
    "extract_token_usage",
]
