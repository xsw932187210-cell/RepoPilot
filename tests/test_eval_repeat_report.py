import copy

import pytest

from repopilot.eval_repeats import REPEAT_PROTOCOL
from repopilot.eval_report import build_eval_report, render_markdown


def fixture():
    records = []
    for case_id, project, difficulty in [("a", "one", "small"), ("b", "two", "broad")]:
        for repeat in (1, 2):
            for mode in ("oneshot", "workflow"):
                success = (case_id == "a") == (mode == "workflow")
                records.append({
                    "case_id": case_id, "project": project, "difficulty": difficulty,
                    "repeat_id": repeat, "run_id": "hand-computable",
                    "cohort_fingerprint": "frozen", "strategy_version": "v1",
                    "mode": mode, "status": "completed", "attempts": 1,
                    "duration_ms": 100, "usage": {
                        "reserved_calls": 2, "model_calls": 2, "started_calls": 2,
                        "successful_model_calls": 2, "token_usage_complete": True,
                        "observed_input_tokens": 2, "observed_output_tokens": 2,
                        "observed_total_tokens": 4,
                    },
                    "result": {"success": success, "iterations": 1},
                })
    return {
        "metadata": {
            "repeat_protocol": REPEAT_PROTOCOL, "fingerprint": "frozen",
            "config": {
                "run_id": "hand-computable", "repeats": 2, "strategy_version": "v1",
                "base": {"modes": ["oneshot", "workflow"]},
            },
            "selected_cases": [
                {"id": "a", "project": "one", "difficulty": "small"},
                {"id": "b", "project": "two", "difficulty": "broad"},
            ],
        },
        "records": records,
    }


def test_hand_computable_paired_project_clusters_and_attempt_columns():
    report = build_eval_report(fixture())
    stats = report["repeat_statistics"]
    assert stats["selected_unique_cases"] == 2
    assert stats["selected_projects"] == 2
    assert stats["selected_units"] == 8
    pair = stats["paired"][0]
    assert pair["mean_case_rate_difference"] == 0
    assert pair["project_cluster_interval"]["interval"] == [-1, 1]
    assert pair["eligible_unique_cases"] == 2
    assert report["modes"]["oneshot"]["success_rate"]["selected_rate"] == 0.5
    assert report["modes"]["workflow"]["executor_attempts"] == 4
    assert report["modes"]["workflow"]["api_requests_started"] == 8
    assert report["modes"]["workflow"]["coder_iterations"] == 4
    assert stats["strata"]["project"]["one"]["workflow"]["successful"] == 2
    assert stats["strata"]["difficulty"]["broad"]["oneshot"]["successful"] == 2
    assert report["summary"]["tokens"]["total_tokens"] == 32
    assert "Unique defects: 2" in render_markdown(report)


@pytest.mark.parametrize("success", [True, False])
def test_all_success_or_all_failure_still_counts_only_unique_defects(success):
    data = fixture()
    for record in data["records"]:
        record["result"]["success"] = success
    report = build_eval_report(data)
    assert report["summary"]["successful"] == (8 if success else 0)
    pair = report["repeat_statistics"]["paired"][0]
    assert pair["mean_case_rate_difference"] == 0
    assert pair["project_cluster_interval"]["degenerate"] is True
    assert report["repeat_statistics"]["selected_unique_cases"] == 2


def test_pending_and_missing_units_keep_selected_denominator_and_prevent_pair_claims():
    data = fixture()
    for record in data["records"]:
        record.update(status="pending_quota", attempts=0, duration_ms=None, result=None, usage={})
    data["records"].pop()
    report = build_eval_report(data)
    assert report["summary"]["selected"] == report["summary"]["pending"] == 8
    assert report["summary"]["duration_ms"]["median"] is None
    assert report["modes"]["workflow"]["pending_by_status"] == {
        "pending_missing": 1, "pending_quota": 3,
    }
    assert report["repeat_statistics"]["paired"][0]["eligible_unique_cases"] == 0
    assert report["repeat_statistics"]["paired"][0]["complete"] is False
    assert report["summary"]["tokens"]["total_tokens"] is None


def test_missing_usage_or_orphan_reserve_never_produces_exact_token_cost():
    data = fixture()
    data["records"][0]["usage"] = {}
    assert build_eval_report(data)["summary"]["tokens"]["total_tokens"] is None
    data = fixture()
    data["metadata"]["budget"] = {"orphan_reservations": 1, "orphan_by_mode": {"oneshot": 1}}
    report = build_eval_report(data)
    assert report["summary"]["tokens"]["observed_total_tokens"] == 32
    assert report["summary"]["tokens"]["total_tokens"] is None
    assert report["modes"]["oneshot"]["tokens"]["total_tokens"] is None
    assert report["repeat_statistics"]["money"] is None


@pytest.mark.parametrize("fault", ["duplicate", "run", "strategy", "cohort"])
def test_duplicate_or_mixed_result_identities_are_rejected(fault):
    data = fixture()
    if fault == "duplicate":
        data["records"].append(copy.deepcopy(data["records"][0]))
    elif fault == "run":
        data["records"][0]["run_id"] = "other"
    elif fault == "strategy":
        data["records"][0]["strategy_version"] = "v2"
    else:
        data["records"][0]["cohort_fingerprint"] = "other"
    with pytest.raises(ValueError):
        build_eval_report(data)


def test_report_omits_hidden_test_identities_and_raw_model_output():
    data = fixture()
    data["records"][0]["result"].update(
        diff="private model output", outcomes={"hidden.py::oracle": "failed"},
        acceptance={"logs": "hidden.py::oracle"},
    )
    rendered = str(build_eval_report(data))
    assert "hidden.py" not in rendered
    assert "private model output" not in rendered


def test_single_project_has_no_cluster_interval():
    data = fixture()
    for case in data["metadata"]["selected_cases"]:
        case["project"] = "one"
    report = build_eval_report(data)
    assert report["repeat_statistics"]["paired"][0]["project_cluster_interval"]["interval"] is None
