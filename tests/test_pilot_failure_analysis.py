import copy
import json
from pathlib import Path

import pytest

from repopilot.pilot_failure_analysis import build_pilot_ledger, main


def _record(**result):
    return {
        "case_id": "public-case", "mode": "auto", "repeat_id": 1, "status": "completed",
        "result": {"success": False, **result},
    }


def test_separates_unchanged_call_failure_from_candidate_quality():
    record = _record(changed_files=[], policy_error_code="model_call_budget_exhausted")
    observation = build_pilot_ledger({"records": [record]})["records"][0]
    assert observation["observation"] == "call_control_stopped_without_change"
    assert observation["root_cause"] == "not_established"


def test_collection_mismatch_is_not_counted_as_behavioral_failure():
    record = _record(changed_files=["source.py"], comparison={"missing_count": 1})
    ledger = build_pilot_ledger({"records": [record]})
    assert ledger["observed_counts"] == {"acceptance_collection_or_identity_failure": 1}


def test_unreviewed_one_shot_does_not_imply_reviewer_approval():
    record = _record(workflow_gate_passed=True, review_performed=False,
                     review={"approved": False, "feedback": ["private-text"]})
    ledger = build_pilot_ledger({"records": [record]})
    assert ledger["records"][0]["observation"] == (
        "visible_gate_passed_independent_acceptance_failed"
    )
    assert ledger["records"][0]["evidence"]["review_performed"] is False
    assert "private-text" not in json.dumps(ledger)


def test_repeat_identity_and_incomplete_denominator_are_preserved():
    first = _record()
    second = copy.deepcopy(first)
    second.update(repeat_id=2, status="pending_quota")
    ledger = build_pilot_ledger({"records": [first, second]})
    assert ledger["selected_units"] == 2
    assert ledger["independent_defects"] == 1
    assert ledger["observed_counts"]["incomplete"] == 1
    with pytest.raises(ValueError, match="Duplicate"):
        build_pilot_ledger({"records": [first, first]})


def test_raw_error_hidden_id_and_patch_never_leave_projection():
    record = _record(diff="private-patch", changed_files=["private-path"],
                     comparison={"regressions": ["hidden-id"]})
    record.update(status="failed", error={"message": "private-error"})
    rendered = json.dumps(build_pilot_ledger({"records": [record]}))
    for forbidden in ("private-patch", "private-path", "hidden-id", "private-error"):
        assert forbidden not in rendered


def test_cli_leaves_original_bytes_untouched(tmp_path: Path):
    source, output = tmp_path / "source.json", tmp_path / "ledger.json"
    original = json.dumps({"records": [_record()]}).encode()
    source.write_bytes(original)
    main([str(source), "--output", str(output)])
    assert source.read_bytes() == original
    assert json.loads(output.read_text())["source_sha256"]
    with pytest.raises(SystemExit):
        main([str(source), "--output", str(source)])
