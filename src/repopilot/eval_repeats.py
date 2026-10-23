"""Versioned repeated evaluations with one durable reservation ceiling."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from repopilot.data_boundary import require_secret_free
from repopilot.eval_runtime import (
    CaseExecutor,
    EvaluationConfig,
    EvaluationRuntime,
    ResumeMismatchError,
)
from repopilot.model_calls import CallBudgetExceeded

REPEAT_PROTOCOL = "repeat-evaluation-v1"


def _digest(value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


def _identifier(value: str) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", value) is not None
    )


@dataclass(frozen=True)
class RepeatConfig:
    base: EvaluationConfig
    run_id: str
    repeats: int
    strategy_version: str
    max_api_attempts: int
    order: str = "rotating-v1"

    def __post_init__(self) -> None:
        if not _identifier(self.run_id) or not _identifier(self.strategy_version):
            raise ValueError("Run and strategy version need portable identities")
        if not all(_identifier(mode) for mode in self.base.modes):
            raise ValueError("Strategy names must be portable")
        if type(self.repeats) is not int or not 1 <= self.repeats <= 100:
            raise ValueError("Repeat count must be between 1 and 100")
        if type(self.max_api_attempts) is not int or self.max_api_attempts < 1:
            raise ValueError("An explicit positive experiment API ceiling is required")
        if self.order != "rotating-v1":
            raise ValueError("Unsupported run order")

    def as_dict(self) -> dict:
        return {
            "protocol": REPEAT_PROTOCOL, "base": self.base.as_dict(),
            "run_id": self.run_id, "repeats": self.repeats,
            "strategy_version": self.strategy_version,
            "max_api_attempts": self.max_api_attempts, "order": self.order,
            "seed": None, "seed_controlled": False,
        }


class ExperimentBudgetExceeded(CallBudgetExceeded):
    def __init__(self, used: int, limit: int) -> None:
        super().__init__(used, limit)
        self.code = "experiment_budget_exhausted"


class RepeatedEvaluationRuntime:
    """One process holds an OS lock; all transport retries reserve before send.

    A shared reservation precedes the per-unit snapshot. If the process dies in
    between, the shared and per-unit ceilings still consume that orphan reserve.
    No transaction or exactly-once claim is made across files/provider transport.
    """

    def __init__(
        self, config: RepeatConfig, record_dir: Path, *,
        reservation_observer=None, **runtime_options: Any,
    ):
        self.config = config
        self.record_dir = record_dir
        self.runtime_options = runtime_options
        self.reservation_observer = reservation_observer
        self.writer = EvaluationRuntime(config.base, record_dir, **runtime_options)
        self.identity: dict = {}
        self.budget: dict = {}

    def _schedule(self, cases: list[dict]) -> list[tuple[int, dict, str]]:
        schedule = []
        modes = self.config.base.modes
        for repeat in range(1, self.config.repeats + 1):
            for index, case in enumerate(cases):
                offset = (repeat - 1 + index) % len(modes)
                for mode in modes[offset:] + modes[:offset]:
                    schedule.append((repeat, case, mode))
        return schedule

    @staticmethod
    def _key(repeat: int, case_id: str, mode: str) -> str:
        return f"repeat-{repeat:03}/{case_id}/{mode}"

    @property
    def fingerprint(self) -> str:
        return _digest(self.identity)

    def _prepare(self, cases: list[dict]) -> None:
        if not cases or len({case["id"] for case in cases}) != len(cases):
            raise ValueError("Repeated evaluation needs unique selected cases")
        if any(not _identifier(case["id"]) for case in cases):
            raise ValueError("Case IDs must be portable")
        require_secret_free(cases)
        self.identity = {
            "schema_version": 1, "config": self.config.as_dict(), "cases": cases,
            "runtime_policy": self.writer.runtime_policy,
            "schedule": [self._key(r, c["id"], m) for r, c, m in self._schedule(cases)],
        }
        path = self.record_dir / "cohort.json"
        expected = {**self.identity, "fingerprint": self.fingerprint}
        if path.exists():
            if json.loads(path.read_text()) != expected:
                raise ResumeMismatchError("Repeated evaluation identity/config differs")
        else:
            if any(p.name != ".run.lock" for p in self.record_dir.iterdir()):
                raise ResumeMismatchError("Non-empty cohort has no identity; refusing reset")
            self.writer._atomic_write(path, expected)
        budget_path = self.record_dir / "api-reservations.json"
        if budget_path.exists():
            budget = json.loads(budget_path.read_text())
            reservations = budget.get("reservations")
            if (
                budget.get("fingerprint") != self.fingerprint
                or budget.get("max_api_attempts") != self.config.max_api_attempts
                or not isinstance(reservations, list)
                or len(reservations) > self.config.max_api_attempts
            ):
                raise ResumeMismatchError("Experiment reservation ledger differs")
            ids: set[str] = set()
            counts: dict[str, int] = {}
            for item in reservations:
                if (
                    not isinstance(item, dict) or not isinstance(item.get("attempt_id"), str)
                    or item["attempt_id"] in ids
                    or item.get("unit") not in self.identity["schedule"]
                ):
                    raise ResumeMismatchError("Invalid experiment reservation")
                ids.add(item["attempt_id"])
                key = item["unit"]
                counts[key] = counts.get(key, 0) + 1
            if any(n > self.config.base.max_model_calls for n in counts.values()):
                raise ResumeMismatchError("Per-unit reservation ceiling exceeded")
            self.budget = budget
        else:
            if (self.record_dir / "runs").exists():
                raise ResumeMismatchError("Missing reservation ledger; refusing budget reset")
            self.budget = {
                "fingerprint": self.fingerprint,
                "max_api_attempts": self.config.max_api_attempts, "reservations": [],
            }
            self.writer._atomic_write(budget_path, self.budget)
        # Detect a missing/truncated shared ledger before any new transport. A
        # larger shared count is an intentionally conservative crash reservation.
        for repeat, case, mode in self._schedule(cases):
            unit = self._key(repeat, case["id"], mode)
            runtime = self._runtime(repeat, mode, unit)
            runtime._prepare_manifest()
            existing = runtime._load_record(case["id"], mode)
            if existing:
                used = int(existing.get("usage", {}).get("reserved_calls", 0))
                reserved = sum(r["unit"] == unit for r in self.budget["reservations"])
                if used > reserved:
                    raise ResumeMismatchError("Shared reservation count lost durable unit usage")

    async def _reserve(self, unit: str, payload: Mapping[str, Any]) -> None:
        reservations = self.budget["reservations"]
        used = sum(item["unit"] == unit for item in reservations)
        if len(reservations) >= self.config.max_api_attempts:
            raise ExperimentBudgetExceeded(len(reservations), self.config.max_api_attempts)
        if used >= self.config.base.max_model_calls:
            raise CallBudgetExceeded(used, self.config.base.max_model_calls)
        allowed = {key: payload[key] for key in (
            "attempt_id", "role", "provider", "model", "retry_index"
        )}
        require_secret_free(allowed)
        updated = {**self.budget, "reservations": [*reservations, {"unit": unit, **allowed}]}
        self.writer._atomic_write(self.record_dir / "api-reservations.json", updated)
        self.budget = updated
        if self.reservation_observer is not None:
            # Persist the cohort first, then mirror its reservation identities in
            # the authorization record before local reservation/transport starts.
            # A crash in this handoff consumes the durable cohort reservation.
            await self.reservation_observer(updated)

    def _runtime(self, repeat: int, mode: str, unit: str) -> EvaluationRuntime:
        base = self.config.base.as_dict()
        child_config = replace(
            self.config.base, modes=(mode,), context_budget=base["context_budget"],
            evaluator_config={
                **base["evaluator_config"], "repeat_protocol": REPEAT_PROTOCOL,
                "run_id": self.config.run_id, "repeat_id": repeat,
                "strategy_version": self.config.strategy_version,
                "cohort_fingerprint": self.fingerprint,
            },
        )

        async def reserve(case_id: str, selected_mode: str, payload: Mapping[str, Any]) -> None:
            del case_id, selected_mode
            await self._reserve(unit, payload)

        return EvaluationRuntime(
            child_config,
            self.record_dir / "runs" / self.config.run_id / f"repeat-{repeat:03}" / mode,
            reservation_hook=reserve, **self.runtime_options,
        )

    async def run(self, cases: list[dict], execute: CaseExecutor) -> dict:
        # Each lock remains held across awaits, including provider waits. Fail fast
        # instead of letting another process reset/reconcile a live owner's files.
        self.record_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.record_dir.chmod(0o700)
        fd = os.open(self.record_dir / ".run.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            cases = json.loads(json.dumps(cases, allow_nan=False))
            self._prepare(cases)
            return await self._run_locked(cases, execute)
        finally:
            os.close(fd)

    async def _run_locked(self, cases: list[dict], execute: CaseExecutor) -> dict:
        records = []
        stop: str | None = None

        async def isolated_execute(case: dict, mode: str, budget: Any) -> Mapping:
            return await execute(json.loads(json.dumps(case)), mode, budget)

        for position, (repeat, case, mode) in enumerate(self._schedule(cases)):
            unit = self._key(repeat, case["id"], mode)
            runtime = self._runtime(repeat, mode, unit)
            runtime._prepare_manifest()
            existing = runtime._load_record(case["id"], mode)
            if existing and existing["status"] in {"completed", "failed"}:
                record = existing
            elif stop or len(self.budget["reservations"]) >= self.config.max_api_attempts:
                reason = stop or "pending_budget"
                record = existing or runtime._pending_record(case["id"], mode)
                record = {
                    **record, "status": reason,
                    "error": {"type": "ExperimentBudgetExceeded", "code": "budget_pending"}
                    if reason == "pending_budget" else record["error"],
                }
                runtime._write_record(record)
            else:
                result = await runtime.run([case], isolated_execute)
                record = result["records"][0]
                if record["status"] == "pending_quota":
                    stop = "pending_quota"
            records.append({
                **record, "run_id": self.config.run_id, "repeat_id": repeat,
                "cohort_fingerprint": self.fingerprint,
                "strategy_version": self.config.strategy_version, "schedule_index": position,
                "project": case.get("project", "unlabeled"),
                "difficulty": case.get("difficulty", "unlabeled"),
            })
        report = {
            "metadata": {
                "repeat_protocol": REPEAT_PROTOCOL, "fingerprint": self.fingerprint,
                "config": self.config.as_dict(), "selected_cases": cases,
                "runtime_policy": self.writer.runtime_policy,
                "budget": {
                    "max_api_attempts": self.config.max_api_attempts,
                    "reserved_api_attempts": len(self.budget["reservations"]),
                    "remaining_api_attempts": self.config.max_api_attempts
                    - len(self.budget["reservations"]),
                    "reserved_transport_retries": sum(
                        r["retry_index"] > 0 for r in self.budget["reservations"]
                    ),
                    "orphan_reservations": len(self.budget["reservations"])
                    - sum(int(r.get("usage", {}).get("reserved_calls", 0)) for r in records),
                    "orphan_by_mode": {
                        mode: sum(r["unit"].endswith(f"/{mode}")
                                  for r in self.budget["reservations"])
                        - sum(int(r.get("usage", {}).get("reserved_calls", 0))
                              for r in records if r["mode"] == mode)
                        for mode in self.config.base.modes
                    },
                },
            },
            "records": records,
        }
        self.writer._atomic_write(self.record_dir / "model-report.json", report)
        return report
