from __future__ import annotations

import json
from pathlib import Path

import pytest

from repopilot.eval_report import build_eval_report, render_markdown


def _record(
    case_id: str,
    mode: str,
    *,
    status: str,
    duration_ms: int | None,
    usage: dict[str, object],
    result: dict[str, object] | None = None,
    error: dict[str, object] | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "case_id": case_id,
        "mode": mode,
        "status": status,
        "duration_ms": duration_ms,
        "usage": usage,
    }
    if result is not None:
        record["result"] = result
    if error is not None:
        record["error"] = error
    return record


def _base_usage(
    *,
    model_calls: int,
    successful_model_calls: int,
    rate_limit_retries: int,
    observed_input_tokens: int,
    observed_output_tokens: int,
    observed_total_tokens: int,
    token_usage_complete: bool,
) -> dict[str, object]:
    if token_usage_complete:
        return {
            "model_calls": model_calls,
            "successful_model_calls": successful_model_calls,
            "rate_limit_retries": rate_limit_retries,
            "transient_retries": 0,
            "max_model_calls": 5,
            "observed_input_tokens": observed_input_tokens,
            "observed_output_tokens": observed_output_tokens,
            "observed_total_tokens": observed_total_tokens,
            "input_tokens": observed_input_tokens,
            "output_tokens": observed_output_tokens,
            "total_tokens": observed_total_tokens,
            "token_usage_complete": True,
        }
    return {
        "model_calls": model_calls,
        "successful_model_calls": successful_model_calls,
        "rate_limit_retries": rate_limit_retries,
        "transient_retries": 0,
        "max_model_calls": 5,
        "observed_input_tokens": observed_input_tokens,
        "observed_output_tokens": observed_output_tokens,
        "observed_total_tokens": observed_total_tokens,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "token_usage_complete": False,
    }


def _source_records() -> list[dict[str, object]]:
    return [
        _record(
            "case-a",
            "oneshot",
            status="completed",
            duration_ms=100,
            usage=_base_usage(
                model_calls=1,
                successful_model_calls=1,
                rate_limit_retries=0,
                observed_input_tokens=10,
                observed_output_tokens=5,
                observed_total_tokens=15,
                token_usage_complete=True,
            ),
            result={
                "success": True,
                "comparison": {
                    "fail_to_pass_count": 2,
                    "fail_to_pass_resolved": 2,
                    "pass_to_pass_count": 2,
                    "regressions": [],
                    "resolved": True,
                },
            },
        ),
        _record(
            "case-a",
            "workflow",
            status="completed",
            duration_ms=300,
            usage=_base_usage(
                model_calls=2,
                successful_model_calls=2,
                rate_limit_retries=0,
                observed_input_tokens=20,
                observed_output_tokens=10,
                observed_total_tokens=30,
                token_usage_complete=True,
            ),
            result={
                "success": False,
                "workflow_gate_passed": True,
                "diff": "SECRET_DIFF",
                "comparison": {
                    "fail_to_pass_count": 1,
                    "fail_to_pass_resolved": 0,
                    "pass_to_pass_count": 4,
                    "regressions": ["hidden-test-a"],
                    "resolved": False,
                },
            },
        ),
        _record(
            "case-b",
            "oneshot",
            status="failed",
            duration_ms=50,
            usage=_base_usage(
                model_calls=2,
                successful_model_calls=1,
                rate_limit_retries=1,
                observed_input_tokens=7,
                observed_output_tokens=4,
                observed_total_tokens=11,
                token_usage_complete=False,
            ),
            error={
                "type": "RuntimeError",
                "message": "authorization: bearer secret-value",
            },
        ),
        _record(
            "case-b",
            "workflow",
            status="pending_quota",
            duration_ms=None,
            usage=_base_usage(
                model_calls=1,
                successful_model_calls=0,
                rate_limit_retries=0,
                observed_input_tokens=6,
                observed_output_tokens=3,
                observed_total_tokens=9,
                token_usage_complete=False,
            ),
            error={"type": "RateLimitError", "message": "daily quota exhausted"},
        ),
    ]


def test_eval_report_aggregates_partial_completion_quota_pending_failures_and_tokens() -> None:
    report = build_eval_report(
        {
            "metadata": {
                "provider": "openai",
                "model": "gpt-5.6-terra",
                "dataset_sha256": "a" * 64,
            },
            "records": _source_records(),
        }
    )

    summary = report["summary"]
    assert summary["selected"] == 4
    assert summary["completed"] == 2
    assert summary["failed"] == 1
    assert summary["pending"] == 1
    assert summary["successful"] == 1
    assert summary["success_rate"]["selected_denominator"] == 4
    assert summary["success_rate"]["completed_denominator"] == 2
    assert summary["success_rate"]["selected_rate"] == pytest.approx(0.25)
    assert summary["success_rate"]["completed_rate"] == pytest.approx(0.5)
    assert summary["hidden_acceptance_regressions"]["tests"] == 1
    assert summary["hidden_acceptance_regressions"]["pass_to_pass_tests"] == 6
    assert summary["hidden_acceptance_regressions"]["regression_rate"] == pytest.approx(1 / 6)
    assert summary["duration_ms"]["median"] == 100
    assert summary["duration_ms"]["p95"] == 300
    assert summary["model_calls"]["total"] == 6
    assert summary["model_calls"]["successful"] == 4
    assert summary["model_calls"]["rate_limit_retries"] == 1
    assert summary["model_calls"]["transient_retries"] == 0
    assert summary["tokens"]["token_usage_complete"] is False
    assert summary["tokens"]["input_tokens"] is None
    assert summary["tokens"]["output_tokens"] is None
    assert summary["tokens"]["total_tokens"] is None
    assert summary["tokens"]["observed_total_tokens"] == 65
    assert summary["failure_reasons"] == {
        "RateLimitError": 1,
        "RuntimeError": 1,
        "hidden_acceptance_regression": 1,
    }

    modes = report["modes"]
    assert modes["oneshot"]["selected"] == 2
    assert modes["oneshot"]["completed"] == 1
    assert modes["oneshot"]["failed"] == 1
    assert modes["oneshot"]["pending"] == 0
    assert modes["oneshot"]["successful"] == 1
    assert modes["oneshot"]["failure_reasons"] == {"RuntimeError": 1}
    assert modes["workflow"]["selected"] == 2
    assert modes["workflow"]["completed"] == 1
    assert modes["workflow"]["failed"] == 0
    assert modes["workflow"]["pending"] == 1
    assert modes["workflow"]["successful"] == 0
    assert modes["workflow"]["failure_reasons"] == {
        "RateLimitError": 1,
        "hidden_acceptance_regression": 1,
    }

    first_case = report["cases"][0]
    assert first_case["case_id"] == "case-a"
    assert first_case["failure_reason"] is None
    assert first_case["result_category"] == "success"
    assert first_case["acceptance_resolved"] is True
    assert first_case["token_usage_complete"] is True
    assert "diff" not in first_case

    regression_case = report["cases"][1]
    assert regression_case["failure_reason"] == "hidden_acceptance_regression"
    assert regression_case["acceptance_regressions"] == 1
    assert "SECRET_DIFF" not in json.dumps(report)
    assert "hidden-test-a" not in json.dumps(report)

    markdown = render_markdown(report)
    assert "SECRET_DIFF" not in markdown
    assert "hidden-test-a" not in markdown
    assert "hidden_acceptance_regression" in markdown


def test_eval_report_loads_model_report_and_record_directory(tmp_path: Path) -> None:
    records = _source_records()
    model_report = tmp_path / "model-report.json"
    model_report.write_text(
        json.dumps(
            {
                "metadata": {
                    "provider": "openai",
                    "model": "gpt-5.6-terra",
                    "dataset_sha256": "a" * 64,
                },
                "records": records,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    record_dir = tmp_path / "model-records"
    record_dir.mkdir()
    (record_dir / "run.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fingerprint": "deadbeef",
                "config": {"provider": "openai", "model": "gpt-5.6-terra"},
                "runtime_policy": {"max_rate_limit_retries": 2, "base_backoff_seconds": 1.0},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cases_dir = record_dir / "cases"
    cases_dir.mkdir()
    for index, record in enumerate(records):
        (cases_dir / f"{index:02d}.json").write_text(
            json.dumps(record, ensure_ascii=False),
            encoding="utf-8",
        )

    report_from_file = build_eval_report(model_report)
    report_from_directory = build_eval_report(record_dir)

    assert report_from_file["summary"] == report_from_directory["summary"]
    assert report_from_directory["metadata"]["fingerprint"] == "deadbeef"
    assert report_from_directory["metadata"]["config"]["model"] == "gpt-5.6-terra"
    assert report_from_directory["summary"]["selected"] == 4


def test_eval_report_reads_duration_from_runtime_result_shape() -> None:
    record = _record(
        "case-a",
        "oneshot",
        status="completed",
        duration_ms=None,
        usage=_base_usage(
            model_calls=1,
            successful_model_calls=1,
            rate_limit_retries=0,
            observed_input_tokens=3,
            observed_output_tokens=2,
            observed_total_tokens=5,
            token_usage_complete=True,
        ),
        result={"success": True, "duration_ms": 432},
    )

    report = build_eval_report([record])

    assert report["summary"]["duration_ms"] == {"median": 432, "p95": 432}
    assert report["cases"][0]["duration_ms"] == 432


def test_eval_report_classifies_missing_acceptance_report_without_logs() -> None:
    record = _record(
        "case-a",
        "workflow",
        status="completed",
        duration_ms=None,
        usage=_base_usage(
            model_calls=2,
            successful_model_calls=2,
            rate_limit_retries=0,
            observed_input_tokens=10,
            observed_output_tokens=4,
            observed_total_tokens=14,
            token_usage_complete=True,
        ),
        result={
            "success": False,
            "duration_ms": 500,
            "acceptance": {"report_missing": True, "logs": "private failure output"},
            "comparison": {"resolved": False, "regressions": []},
        },
    )

    report = build_eval_report([record])

    assert report["summary"]["failure_reasons"] == {"acceptance_report_missing": 1}
    assert "private failure output" not in json.dumps(report)


def test_eval_report_excludes_unattempted_quota_placeholders_from_latency() -> None:
    usage = _base_usage(
        model_calls=0,
        successful_model_calls=0,
        rate_limit_retries=0,
        observed_input_tokens=0,
        observed_output_tokens=0,
        observed_total_tokens=0,
        token_usage_complete=False,
    )
    completed = _record(
        "case-a",
        "oneshot",
        status="completed",
        duration_ms=100,
        usage=usage,
        result={"success": True},
    )
    completed["attempts"] = 1
    placeholder = _record(
        "case-b",
        "oneshot",
        status="pending_quota",
        duration_ms=None,
        usage=usage,
        error={"type": "RateLimitError", "message": "run stopped by provider quota"},
    )
    placeholder["attempts"] = 0

    report = build_eval_report([completed, placeholder])

    assert report["summary"]["duration_ms"] == {"median": 100, "p95": 100}


def test_eval_report_accepts_count_only_sanitized_comparison() -> None:
    record = _record(
        "case-a",
        "workflow",
        status="completed",
        duration_ms=200,
        usage=_base_usage(
            model_calls=3,
            successful_model_calls=3,
            rate_limit_retries=0,
            observed_input_tokens=10,
            observed_output_tokens=5,
            observed_total_tokens=15,
            token_usage_complete=True,
        ),
        result={
            "success": False,
            "comparison": {
                "pass_to_pass_count": 5,
                "regression_count": 2,
                "resolved": False,
            },
        },
    )

    report = build_eval_report([record])

    assert report["summary"]["hidden_acceptance_regressions"]["tests"] == 2
    assert report["summary"]["failure_reasons"] == {"hidden_acceptance_regression": 1}
    assert report["cases"][0]["acceptance_regressions"] == 2
