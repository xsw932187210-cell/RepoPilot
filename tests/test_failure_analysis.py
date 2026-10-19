from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from repopilot.failure_analysis import build_failure_ledger, main

_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE = Path(__file__).parent / "fixtures" / "failure-observations-2026-09-09.json"


def _fixture() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_historical_observations_keep_independent_acceptance_and_runtime_failures_separate() -> (
    None
):
    ledger = build_failure_ledger(_fixture())
    summary = ledger["summary"]
    assert summary["selected"] == 6
    assert summary["completed"] == 5
    assert summary["repaired"] == 2
    assert summary["runtime_failed"] == 1
    assert summary["terminal_unrepaired"] == 4
    assert summary["unknown_root_causes"] == 4
    assert summary["model_calls"] == 17
    assert summary["observed_failure_counts"] == {
        "acceptance_protocol_failure": 1,
        "behavior_mismatch": 2,
        "policy_rejection": 1,
    }
    assert ledger["paired"] == {
        "both_repaired": 0,
        "oneshot_only": 1,
        "workflow_only": 1,
        "neither_repaired": 0,
        "unpaired": 2,
        "pending": 0,
    }
    assert ledger["workflow_observations"] == {
        "gate_passed_but_acceptance_failed": 1,
        "repaired_with_final_reviewer_disapproval": 1,
    }


def test_missing_report_with_regression_is_counted_once_without_blame_on_reviewer() -> None:
    records = build_failure_ledger(_fixture())["records"]
    item = next(r for r in records if (r["case_id"], r["mode"]) == ("thefuck-002", "workflow"))
    assert item["primary_failure"] == "acceptance_protocol_failure"
    assert item["evidence"]["regression_count"] == 1
    assert item["evidence"]["missing_count"] == 2
    assert item["evidence"]["iterations"] == 2
    assert item["evidence"]["review_risk_level"] == "high"
    assert item["root_cause"] == "unknown"


@pytest.mark.parametrize("status", ["pending_quota", "running"])
def test_nonterminal_partner_is_neither_a_failure_nor_a_paired_win(status: str) -> None:
    payload = {
        "records": [
            {
                "case_id": "pending-case",
                "mode": "oneshot",
                "status": "completed",
                "result": {"success": True},
            },
            {"case_id": "pending-case", "mode": "workflow", "status": status},
        ]
    }
    ledger = build_failure_ledger(payload)
    assert ledger["summary"]["selected"] == 2
    assert ledger["summary"]["terminal_unrepaired"] == 0
    assert ledger["summary"]["unknown_root_causes"] == 0
    assert ledger["summary"]["observed_failure_counts"] == {}
    assert ledger["paired"]["pending"] == 1
    assert ledger["paired"]["oneshot_only"] == 0
    assert ledger["paired"]["neither_repaired"] == 0


def test_free_text_and_hidden_test_identities_cannot_enter_the_export() -> None:
    private_text = "unreviewed-private-text"
    payload = {
        "metadata": {"provider_response": private_text},
        "records": [
            {
                "case_id": "public-case",
                "mode": "workflow",
                "status": "completed",
                "result": {
                    "success": False,
                    "diff": private_text,
                    "acceptance": {"stdout": private_text, "stderr": private_text},
                    "comparison": {"regressions": ["hidden-test-identity"]},
                    "review": {"feedback": [private_text]},
                },
            }
        ],
    }
    ledger = build_failure_ledger(payload)
    rendered = json.dumps(ledger)
    assert private_text not in rendered
    assert "hidden-test-identity" not in rendered
    assert ledger["records"][0]["primary_failure"] == "candidate_regression"
    assert ledger["records"][0]["evidence"]["regression_count"] == 1
    payload["records"][0]["status"] = "failed"
    payload["records"][0]["error"] = {"type": private_text, "message": private_text}
    ledger = build_failure_ledger(payload)
    assert private_text not in json.dumps(ledger)
    assert ledger["records"][0]["primary_failure"] == "runtime_failure_unknown"


def test_duplicate_case_mode_cannot_inflate_the_denominator() -> None:
    payload = _fixture()
    payload["records"].append(copy.deepcopy(payload["records"][0]))
    with pytest.raises(ValueError, match="duplicate case-mode"):
        build_failure_ledger(payload)


def test_mixed_experiment_records_are_rejected() -> None:
    payload = _fixture()
    payload["records"][0]["fingerprint"] = "a" * 64
    with pytest.raises(ValueError, match="different experiments"):
        build_failure_ledger(payload)


def test_completed_without_a_boolean_outcome_is_not_assumed_repaired() -> None:
    payload = _fixture()
    payload["records"][0]["result"]["success"] = "true"
    with pytest.raises(ValueError, match="boolean success"):
        build_failure_ledger(payload)


def test_cli_records_the_source_digest_without_overwriting_the_source(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    original = json.dumps(_fixture()).encode()
    source.write_bytes(original)
    output = tmp_path / "evidence" / "ledger.json"
    main([str(source), "--output", str(output)])
    assert source.read_bytes() == original
    ledger = json.loads(output.read_text())
    assert ledger["source_sha256"] == hashlib.sha256(original).hexdigest()
    with pytest.raises(SystemExit):
        main([str(source), "--output", str(source)])
    assert source.read_bytes() == original


def test_committed_ledger_preserves_the_original_forty_record_denominator() -> None:
    ledger = json.loads(
        (_ROOT / "docs" / "evidence" / "2026-09-09-failure-ledger.json").read_text()
    )
    records = ledger["records"]
    assert len(records) == 40
    assert len({(r["case_id"], r["mode"]) for r in records}) == 40
    assert len({r["case_id"] for r in records}) == 20
    assert sum(r["outcome"] == "repaired" for r in records) == 28
    assert sum(r["primary_failure"] == "behavior_mismatch" for r in records) == 9
    assert sum(r["primary_failure"] == "acceptance_protocol_failure" for r in records) == 1
    assert sum(r["primary_failure"] == "policy_rejection" for r in records) == 2
    assert ledger["by_mode"]["oneshot"]["repaired"] == 15
    assert ledger["by_mode"]["workflow"]["repaired"] == 13
    assert ledger["summary"]["model_calls"] == 91
    assert ledger["paired"] == {
        "both_repaired": 12,
        "oneshot_only": 3,
        "workflow_only": 1,
        "neither_repaired": 4,
        "unpaired": 0,
        "pending": 0,
    }
