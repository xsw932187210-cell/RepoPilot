"""Read-only, allowlisted observations for repeated real-model experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from repopilot.data_boundary import require_secret_free

_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}\Z")
_CODES = {
    "model_call_budget_exhausted", "model_provider_transient_failure",
    "model_provider_non_retryable_failure", "model_response_size_exceeded",
}


def _object(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def build_pilot_ledger(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Classify observed stop points, never infer patches or hidden test identities."""
    observations = []
    seen = set()
    for record in payload["records"]:
        case, mode, repeat = record["case_id"], record["mode"], record["repeat_id"]
        if not isinstance(case, str) or not _ID.fullmatch(case):
            raise ValueError("Invalid public case identity")
        if mode not in {"oneshot", "workflow", "auto"} or type(repeat) is not int or repeat < 1:
            raise ValueError("Invalid strategy or repeat identity")
        identity = (case, repeat, mode)
        if identity in seen:
            raise ValueError("Duplicate case/repeat/strategy")
        seen.add(identity)
        status = record["status"]
        if status not in {"completed", "failed", "running", "pending_quota"}:
            raise ValueError("Unknown execution status")
        result, usage = _object(record.get("result")), _object(record.get("usage"))
        comparison = _object(result.get("comparison"))
        acceptance = _object(result.get("acceptance"))
        review = _object(result.get("review"))
        changes = result.get("changed_files")
        changed = len(changes) if isinstance(changes, list) else None
        policy_retry = _count(result.get("policy_retry_count"))
        code = result.get("policy_error_code")
        code = code if code in _CODES else None
        if status == "completed" and type(result.get("success")) is not bool:
            raise ValueError("Completed unit requires boolean success")
        if status in {"running", "pending_quota"}:
            observation = "incomplete"
        elif status == "failed":
            observation = "runtime_error_without_acceptance"
        elif result["success"]:
            observation = "repaired"
        elif changed == 0 and code:
            observation = "call_control_stopped_without_change"
        elif changed == 0 and policy_retry:
            observation = "write_intent_rejected_without_change"
        elif acceptance.get("report_missing") is True or any(
            (_count(comparison.get(key)) or 0) > 0
            for key in ("missing_count", "extra_count", "baseline_identity_drift_count")
        ):
            observation = "acceptance_collection_or_identity_failure"
        elif result.get("workflow_gate_passed") is True:
            observation = "visible_gate_passed_independent_acceptance_failed"
        elif code == "model_call_budget_exhausted" and changed:
            observation = "budget_exhausted_with_change"
        else:
            observation = "unrepaired_unclassified"
        facts = {
            "changed_file_count": changed,
            "policy_retry_count": policy_retry,
            "policy_error_code": code,
            "workflow_gate_passed": result.get("workflow_gate_passed")
            if type(result.get("workflow_gate_passed")) is bool else None,
            "review_performed": result.get("review_performed")
            if type(result.get("review_performed")) is bool else None,
            "review_approved": review.get("approved")
            if type(review.get("approved")) is bool else None,
            "acceptance_exit_code": acceptance.get("exit_code")
            if type(acceptance.get("exit_code")) is int else None,
        }
        for key in ("fail_to_pass_count", "fail_to_pass_resolved", "pass_to_pass_count",
                    "regression_count", "missing_count", "extra_count"):
            facts[key] = _count(comparison.get(key))
        for key in ("model_calls", "successful_model_calls", "failed_calls", "unknown_calls"):
            facts[key] = _count(usage.get(key))
        observations.append({
            "case_id": case, "repeat_id": repeat, "mode": mode, "status": status,
            "observation": observation, "root_cause": "not_established",
            "evidence": {key: value for key, value in facts.items() if value is not None},
        })
    observations.sort(key=lambda r: (r["case_id"], r["repeat_id"], r["mode"]))
    counts = Counter(r["observation"] for r in observations)
    ledger = {
        "schema_version": "pilot-failure-observations-v1",
        "selected_units": len(observations),
        "independent_defects": len({r["case_id"] for r in observations}),
        "observed_counts": dict(sorted(counts.items())),
        "root_causes_established": 0,
        "model_requests_issued": 0,
        "records": observations,
    }
    require_secret_free(ledger)
    return ledger


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.source.resolve() == args.output.resolve():
        parser.error("Output must not overwrite the experiment")
    original = args.source.read_bytes()
    ledger = build_pilot_ledger(json.loads(original))
    ledger["source_sha256"] = hashlib.sha256(original).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in ledger.items() if key != "records"}))


if __name__ == "__main__":
    main()
