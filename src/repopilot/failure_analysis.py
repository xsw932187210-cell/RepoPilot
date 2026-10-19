"""Offline failure evidence; observed outcomes never imply a model's root cause."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_STATUSES = {"completed", "failed", "pending_quota", "running"}
_MODES = {"oneshot", "workflow", "auto"}
_PUBLIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _count(value: object) -> int | None:
    if isinstance(value, list):
        return len(value)
    number = _integer(value)
    return number if number is not None and number >= 0 else None


def _digest(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("invalid experiment digest")
    return value


def _evidence(record: Mapping[str, Any]) -> dict[str, Any]:
    result = _mapping(record.get("result"))
    acceptance = _mapping(result.get("acceptance"))
    comparison = _mapping(result.get("comparison"))
    review = _mapping(result.get("review"))
    usage = _mapping(record.get("usage"))
    risk = review.get("risk_level")
    diff = result.get("diff")
    facts: dict[str, Any] = {
        "workflow_gate_passed": _boolean(result.get("workflow_gate_passed")),
        "has_change": bool(diff.strip()) if isinstance(diff, str) else None,
        "iterations": _count(result.get("iterations")),
        "policy_retry_count": _count(result.get("policy_retry_count")),
        "acceptance_exit_code": _integer(acceptance.get("exit_code")),
        "acceptance_report_missing": _boolean(acceptance.get("report_missing")),
        "acceptance_timed_out": _boolean(acceptance.get("timed_out")),
        "review_approved": _boolean(review.get("approved")),
        "review_risk_level": risk if risk in ("low", "medium", "high") else None,
        "model_calls": _count(usage.get("model_calls")),
        "duration_ms": _count(record.get("duration_ms")),
    }
    for key in ("fail_to_pass_count", "fail_to_pass_resolved", "pass_to_pass_count"):
        facts[key] = _count(comparison.get(key))
    for key, legacy in {
        "regression_count": "regressions",
        "missing_count": "missing_tests",
        "extra_count": "extra_tests",
        "baseline_identity_drift_count": "baseline_identity_drift",
    }.items():
        value = comparison.get(key)
        facts[key] = _count(value if value is not None else comparison.get(legacy))
    return {key: value for key, value in facts.items() if value is not None}


def _failure_class(record: Mapping[str, Any], facts: Mapping[str, Any]) -> str:
    if record["status"] == "failed":
        if _mapping(record.get("error")).get("type") == "SecurityError":
            return "policy_rejection"
        return "runtime_failure_unknown"
    if facts.get("acceptance_report_missing") is True:
        return "acceptance_protocol_failure"
    if facts.get("acceptance_timed_out") is True:
        return "acceptance_timeout"
    if any(
        facts.get(key, 0) > 0
        for key in ("missing_count", "extra_count", "baseline_identity_drift_count")
    ):
        return "acceptance_protocol_failure"
    if facts.get("regression_count", 0) > 0:
        return "candidate_regression"
    expected = facts.get("fail_to_pass_count")
    resolved = facts.get("fail_to_pass_resolved")
    if expected is not None and resolved is not None and resolved < expected:
        return "behavior_mismatch"
    if facts.get("has_change") is False:
        return "empty_change"
    if facts.get("workflow_gate_passed") is False:
        return "workflow_gate_failure"
    return "unknown"


def _summarize(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    outcomes = Counter(record["outcome"] for record in records)
    failures = Counter(
        record["primary_failure"] for record in records if record["primary_failure"] is not None
    )
    calls = [record["evidence"].get("model_calls") for record in records]
    return {
        "selected": len(records),
        "completed": sum(record["status"] == "completed" for record in records),
        "repaired": outcomes["repaired"],
        "runtime_failed": outcomes["runtime_failure"],
        "quota_pending": outcomes["quota_pending"],
        "incomplete": outcomes["incomplete"],
        "terminal_unrepaired": sum(failures.values()),
        "unknown_root_causes": sum(record["root_cause"] == "unknown" for record in records),
        "observed_failure_counts": dict(sorted(failures.items())),
        "model_calls": sum(calls) if all(call is not None for call in calls) else None,
    }


def _paired(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    by_case: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for record in records:
        by_case[record["case_id"]][record["mode"]] = record
    counts = dict.fromkeys(
        (
            "both_repaired",
            "oneshot_only",
            "workflow_only",
            "neither_repaired",
            "unpaired",
            "pending",
        ),
        0,
    )
    for modes in by_case.values():
        if "oneshot" not in modes or "workflow" not in modes:
            counts["unpaired"] += 1
            continue
        pair = (modes["oneshot"], modes["workflow"])
        if any(record["status"] not in {"completed", "failed"} for record in pair):
            counts["pending"] += 1
            continue
        first, second = (record["outcome"] == "repaired" for record in pair)
        key = (
            "both_repaired"
            if first and second
            else "oneshot_only"
            if first
            else "workflow_only"
            if second
            else "neither_repaired"
        )
        counts[key] += 1
    return counts


def build_failure_ledger(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Extract count-only evidence, rejecting duplicates and mixed experiment identities.

    Free text, test identities, file paths, diffs, prompts and provider responses
    are deliberately absent. A failure class describes an observation; root cause
    remains unknown without an independently reviewed causal investigation.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("a report object is required")
    raw_records = payload.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        raise ValueError("a non-empty records list is required")
    metadata = _mapping(payload.get("metadata"))
    config = _mapping(metadata.get("config"))
    evaluator = _mapping(config.get("evaluator_config"))
    fingerprint = _digest(metadata.get("fingerprint"))
    seen: set[tuple[str, str]] = set()
    fingerprints = {fingerprint} if fingerprint else set()
    records = []
    for index, raw in enumerate(raw_records):
        if not isinstance(raw, Mapping):
            raise ValueError("every record must be an object")
        case_id, mode, status = raw.get("case_id"), raw.get("mode"), raw.get("status")
        if not isinstance(case_id, str) or _PUBLIC_ID.fullmatch(case_id) is None:
            raise ValueError("case_id must be a public dataset identifier")
        if not isinstance(mode, str) or mode not in _MODES:
            raise ValueError("unsupported evaluation mode")
        if not isinstance(status, str) or status not in _STATUSES:
            raise ValueError("unsupported evaluation status")
        key = (case_id, mode)
        if key in seen:
            raise ValueError("duplicate case-mode record")
        seen.add(key)
        record_fingerprint = _digest(raw.get("fingerprint"))
        if record_fingerprint:
            fingerprints.add(record_fingerprint)
        if len(fingerprints) > 1:
            raise ValueError("records belong to different experiments")
        success = _boolean(_mapping(raw.get("result")).get("success"))
        if status == "completed" and success is None:
            raise ValueError("completed records require a boolean success outcome")
        outcome = (
            "repaired"
            if status == "completed" and success
            else "acceptance_failure"
            if status == "completed"
            else "runtime_failure"
            if status == "failed"
            else "quota_pending"
            if status == "pending_quota"
            else "incomplete"
        )
        facts = _evidence(raw)
        failed = outcome in {"acceptance_failure", "runtime_failure"}
        records.append(
            {
                "source_record_index": index,
                "case_id": case_id,
                "mode": mode,
                "status": status,
                "outcome": outcome,
                "primary_failure": _failure_class(raw, facts) if failed else None,
                "root_cause": "unknown" if failed else None,
                "evidence": facts,
            }
        )
    records.sort(key=lambda record: (record["case_id"], record["mode"]))
    workflow = [record for record in records if record["mode"] == "workflow"]
    return {
        "schema_version": 1,
        "unit": "case_mode",
        "experiment": {
            "fingerprint": next(iter(fingerprints), None),
            "dataset_sha256": _digest(config.get("dataset_sha256")),
            "implementation_sha256": _digest(evaluator.get("implementation_sha256")),
            "evaluator_version": _count(evaluator.get("version")),
        },
        "summary": _summarize(records),
        "by_mode": {
            mode: _summarize([record for record in records if record["mode"] == mode])
            for mode in sorted({record["mode"] for record in records})
        },
        "paired": _paired(records),
        "workflow_observations": {
            "gate_passed_but_acceptance_failed": sum(
                record["outcome"] == "acceptance_failure"
                and record["evidence"].get("workflow_gate_passed") is True
                for record in workflow
            ),
            "repaired_with_final_reviewer_disapproval": sum(
                record["outcome"] == "repaired"
                and record["evidence"].get("review_approved") is False
                for record in workflow
            ),
        },
        "records": records,
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build an offline, count-only failure ledger")
    parser.add_argument("input", type=Path, help="Historical model-report.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.input.resolve() == args.output.resolve():
        parser.error("input and output must be different files")
    source = args.input.read_bytes()
    ledger = build_failure_ledger(json.loads(source))
    ledger["source_sha256"] = hashlib.sha256(source).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {ledger['summary']['selected']} case-mode observations")


if __name__ == "__main__":
    main()
