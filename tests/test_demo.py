import json

import pytest

from repopilot.demo import main, run_demo


@pytest.mark.parametrize("decision,status", [("approve", "completed"), ("reject", "cancelled")])
async def test_scripted_demo_has_actual_failed_tests_and_bound_resume(
    decision, status, monkeypatch,
):
    monkeypatch.setenv("MODEL_PROVIDER", "google_cloud")
    monkeypatch.setenv("REVIEW_MODEL_PROVIDER", "chatgpt")
    monkeypatch.setenv("GOOGLE_CLOUD_API_KEY", "synthetic-do-not-export-this-key")
    monkeypatch.setenv("GITHUB_WRITE_ENABLED", "true")
    report, diff = await run_demo(decision)
    assert report["ok"]
    assert report["status"] == status
    assert report["test_runs"][0]["exit_code"] == 1
    assert report["test_runs"][1]["exit_code"] == 0
    assert report["candidate_binding_unchanged"]
    assert report["role_calls_before_resume"] == report["role_calls_after_resume"]
    assert report["node_runs"]["test_runner"] == 2
    assert report["external_model_requests"] == 0
    assert report["publication_enabled"] is False
    assert report["pull_request_url"] is None
    assert report["worker_process_restart_verified_here"] is False
    assert "return a + b" in diff
    assert "synthetic-do-not-export-this-key" not in json.dumps(report) + diff


def test_demo_cli_default_only_exports_the_approval_checkpoint(tmp_path):
    main(["--output", str(tmp_path)])
    report_path, = tmp_path.glob("*/summary.json")
    report = json.loads(report_path.read_text())
    assert report["ok"]
    assert report["status"] == "awaiting_approval"
    assert report["resumed"] is False
    assert report["node_runs"].get("finalize", 0) == 0
    assert (report_path.parent / "candidate.diff").is_file()
