from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
_STATUS_COMPLETED = "completed"
_STATUS_FAILED = "failed"
_STATUS_PENDING = {"pending_quota", "running"}
_RECORD_KEYS = ("records", "model_records", "case_records")


def _is_pathlike(value: object) -> bool:
    return isinstance(value, (str, Path))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_directory(source: Path) -> dict[str, Any]:
    report_path = source / "model-report.json"
    if report_path.exists():
        payload = _read_json(report_path)
        if isinstance(payload, Mapping):
            return dict(payload)
        return {"records": payload}

    if source.name == "cases" and (source.parent / "run.json").exists():
        run_path = source.parent / "run.json"
        cases_dir = source
    else:
        run_path = source / "run.json"
        cases_dir = source / "cases"

    payload: dict[str, Any] = {}
    if run_path.exists():
        run = _read_json(run_path)
        if isinstance(run, Mapping):
            payload.update(run)
        else:
            payload["metadata"] = {"run": run}

    if cases_dir.is_dir():
        payload["records"] = [_read_json(path) for path in sorted(cases_dir.glob("*.json"))]
    return payload


def _load_source(source: Any) -> dict[str, Any] | list[Any]:
    if isinstance(source, Mapping):
        return dict(source)
    if isinstance(source, Sequence) and not isinstance(source, (str, bytes, bytearray, Path)):
        return list(source)
    if _is_pathlike(source):
        path = Path(source)
        if path.is_dir():
            return _load_directory(path)
        payload = _read_json(path)
        if isinstance(payload, Mapping):
            return dict(payload)
        return payload
    raise TypeError("source must be a path, mapping, or sequence of records")


def _extract_records(payload: Mapping[str, Any] | Sequence[Any]) -> list[dict[str, Any]]:
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        return [dict(item) for item in payload if isinstance(item, Mapping)]

    for key in _RECORD_KEYS:
        value = payload.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [dict(item) for item in value if isinstance(item, Mapping)]

    if isinstance(payload.get("status"), str) and isinstance(
        payload.get("case_id") or payload.get("id") or payload.get("name"), str
    ):
        return [dict(payload)]

    raise ValueError("could not locate model records in the provided source")


def _collect_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    source_metadata = payload.get("metadata")
    if isinstance(source_metadata, Mapping):
        metadata.update(source_metadata)
    for key in ("schema_version", "fingerprint", "config", "runtime_policy", "record_dir"):
        if key in payload and key not in metadata:
            metadata[key] = payload[key]
    return metadata


def _number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _percentile(values: list[int], quantile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * quantile)
    return ordered[index]


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _safe_error_type(record: Mapping[str, Any]) -> str:
    error = record.get("error")
    if isinstance(error, Mapping):
        error_type = error.get("type")
        if isinstance(error_type, str) and error_type.strip():
            return error_type
    if isinstance(error, BaseException):
        return type(error).__name__
    status = record.get("status")
    if isinstance(status, str) and status.strip():
        return status
    return "unknown"


def _comparison(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    result = record.get("result")
    if isinstance(result, Mapping):
        comparison = result.get("comparison")
        if isinstance(comparison, Mapping):
            return comparison
    return None


def _result_category(record: Mapping[str, Any]) -> str:
    result = record.get("result")
    if not isinstance(result, Mapping):
        return "unsuccessful_result"
    acceptance = result.get("acceptance")
    if isinstance(acceptance, Mapping) and acceptance.get("report_missing") is True:
        return "acceptance_report_missing"
    for key in ("category", "reason", "kind", "outcome", "type"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value

    comparison = _comparison(record)
    if comparison is not None:
        regression_count = _number(comparison.get("regression_count"))
        regressions = comparison.get("regressions")
        if isinstance(regressions, Sequence) and not isinstance(
            regressions, (str, bytes, bytearray)
        ):
            regression_count = len(regressions)
        if regression_count:
            return "hidden_acceptance_regression"
        if comparison.get("resolved") is False:
            return "acceptance_mismatch"

    if result.get("workflow_gate_passed") is False:
        return "workflow_gate_not_passed"

    if isinstance(acceptance, Mapping):
        exit_code = acceptance.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            return f"acceptance_exit_code_{exit_code}"
        if exit_code is not None and not isinstance(exit_code, int):
            return "acceptance_failed"

    diff = result.get("diff")
    if isinstance(diff, str) and not diff.strip():
        return "empty_change"

    if result.get("success") is False:
        return "unsuccessful_result"
    return "success"


def _model_call_count(record: Mapping[str, Any], field: str) -> int:
    usage = record.get("usage")
    if isinstance(usage, Mapping):
        value = _number(usage.get(field))
        if value is not None:
            return value
    return 0


def _duration_ms(record: Mapping[str, Any]) -> int | None:
    duration = _number(record.get("duration_ms"))
    if duration is not None:
        return duration
    result = record.get("result")
    return _number(result.get("duration_ms")) if isinstance(result, Mapping) else None


def _token_summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    observed_input = 0
    observed_output = 0
    observed_total = 0
    complete = bool(records)
    for record in records:
        usage = record.get("usage")
        if not isinstance(usage, Mapping):
            complete = False
            continue
        observed_input += _number(usage.get("observed_input_tokens")) or 0
        observed_output += _number(usage.get("observed_output_tokens")) or 0
        observed_total += _number(usage.get("observed_total_tokens")) or 0
        complete = complete and bool(usage.get("token_usage_complete"))

    if complete:
        return {
            "token_usage_complete": True,
            "input_tokens": observed_input,
            "output_tokens": observed_output,
            "total_tokens": observed_total,
            "observed_input_tokens": observed_input,
            "observed_output_tokens": observed_output,
            "observed_total_tokens": observed_total,
        }
    return {
        "token_usage_complete": False,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "observed_input_tokens": observed_input,
        "observed_output_tokens": observed_output,
        "observed_total_tokens": observed_total,
    }


def _failure_reason(record: Mapping[str, Any]) -> str | None:
    status = record.get("status")
    if status == _STATUS_COMPLETED:
        result = record.get("result")
        if isinstance(result, Mapping) and bool(result.get("success")):
            return None
        return _result_category(record)
    if status == _STATUS_FAILED:
        return _safe_error_type(record)
    if isinstance(status, str) and status in _STATUS_PENDING:
        return _safe_error_type(record)
    if isinstance(status, str) and status.strip():
        return status
    return "unknown"


def _safe_case_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    result = record.get("result")
    usage = record.get("usage")
    comparison = _comparison(record)
    summary: dict[str, Any] = {
        "case_id": record.get("case_id"),
        "mode": record.get("mode"),
        "status": record.get("status"),
        "duration_ms": _duration_ms(record),
        "model_calls": _number(usage.get("model_calls")) if isinstance(usage, Mapping) else None,
        "successful_model_calls": (
            _number(usage.get("successful_model_calls")) if isinstance(usage, Mapping) else None
        ),
        "token_usage_complete": (
            bool(usage.get("token_usage_complete")) if isinstance(usage, Mapping) else None
        ),
        "success": bool(result.get("success")) if isinstance(result, Mapping) else False,
        "failure_reason": _failure_reason(record),
    }
    if isinstance(result, Mapping):
        summary["result_category"] = _result_category(record)
    if comparison is not None:
        regression_count = _number(comparison.get("regression_count"))
        regressions = comparison.get("regressions")
        if isinstance(regressions, Sequence) and not isinstance(
            regressions, (str, bytes, bytearray)
        ):
            regression_count = len(regressions)
        summary["acceptance_regressions"] = regression_count or 0
        summary["acceptance_pass_to_pass"] = _number(comparison.get("pass_to_pass_count"))
        summary["acceptance_resolved"] = bool(comparison.get("resolved"))
    return summary


def _summarize_records(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    selected = len(records)
    completed_records = [record for record in records if record.get("status") == _STATUS_COMPLETED]
    failed_records = [record for record in records if record.get("status") == _STATUS_FAILED]
    pending_records = [
        record
        for record in records
        if record.get("status") not in {_STATUS_COMPLETED, _STATUS_FAILED}
    ]
    successful_records = [
        record
        for record in completed_records
        if isinstance(record.get("result"), Mapping) and bool(record["result"].get("success"))
    ]

    duration_records = [
        record
        for record in records
        if record.get("status") != "running"
        and (_number(record.get("attempts")) != 0 or "attempts" not in record)
    ]
    durations = [_duration_ms(record) for record in duration_records]
    durations = [value for value in durations if value is not None]
    failure_reasons = Counter(
        reason for record in records if (reason := _failure_reason(record)) is not None
    )

    comparison_records = [record for record in records if _comparison(record) is not None]
    regression_tests = 0
    pass_to_pass_tests = 0
    resolved_cases = 0
    for record in comparison_records:
        comparison = _comparison(record)
        if comparison is None:
            continue
        regression_count = _number(comparison.get("regression_count"))
        regressions = comparison.get("regressions")
        if isinstance(regressions, Sequence) and not isinstance(
            regressions, (str, bytes, bytearray)
        ):
            regression_count = len(regressions)
        regression_tests += regression_count or 0
        pass_to_pass_tests += _number(comparison.get("pass_to_pass_count")) or 0
        resolved_cases += int(bool(comparison.get("resolved")))

    return {
        "selected": selected,
        "completed": len(completed_records),
        "failed": len(failed_records),
        "pending": len(pending_records),
        "successful": len(successful_records),
        "success_rate": {
            "successful": len(successful_records),
            "selected_denominator": selected,
            "completed_denominator": len(completed_records),
            "selected_rate": _rate(len(successful_records), selected),
            "completed_rate": _rate(len(successful_records), len(completed_records)),
        },
        "hidden_acceptance_regressions": {
            "tests": regression_tests,
            "pass_to_pass_tests": pass_to_pass_tests,
            "regression_rate": _rate(regression_tests, pass_to_pass_tests),
            "resolved_cases": resolved_cases,
            "comparison_cases": len(comparison_records),
        },
        "duration_ms": {
            "median": int(statistics.median(durations)) if durations else None,
            "p95": _percentile(durations, 0.95),
        },
        "model_calls": {
            "total": sum(_model_call_count(record, "model_calls") for record in records),
            "successful": sum(
                _model_call_count(record, "successful_model_calls") for record in records
            ),
            "rate_limit_retries": sum(
                _model_call_count(record, "rate_limit_retries") for record in records
            ),
            "transient_retries": sum(
                _model_call_count(record, "transient_retries") for record in records
            ),
            "max_model_calls": max(
                (_model_call_count(record, "max_model_calls") for record in records),
                default=None,
            ),
        },
        "tokens": _token_summary(records),
        "failure_reasons": dict(sorted(failure_reasons.items())),
    }


def build_eval_report(source: Any) -> dict[str, Any]:
    payload = _load_source(source)
    metadata: dict[str, Any] = {}
    if isinstance(payload, Mapping):
        metadata = _collect_metadata(payload)
    records = _extract_records(payload)

    modes = sorted(
        {str(record.get("mode")) for record in records if record.get("mode") is not None}
    )
    mode_reports = {
        mode: _summarize_records([record for record in records if record.get("mode") == mode])
        for mode in modes
    }

    overall = _summarize_records(records)
    return {
        "schema_version": SCHEMA_VERSION,
        "metadata": metadata,
        "summary": overall,
        "modes": mode_reports,
        "cases": [_safe_case_summary(record) for record in records],
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    modes = report.get("modes", {})
    lines = ["# RepoPilot Evaluation Report", ""]
    lines.append("## Overall")
    lines.extend(
        [
            "| Metric | Value |",
            "| --- | ---: |",
            f"| Selected | {summary['selected']} |",
            f"| Completed | {summary['completed']} |",
            f"| Failed | {summary['failed']} |",
            f"| Pending | {summary['pending']} |",
            f"| Successful | {summary['successful']} |",
            f"| Success rate (selected) | {summary['success_rate']['selected_rate']} |",
            f"| Success rate (completed) | {summary['success_rate']['completed_rate']} |",
            f"| Hidden regressions | {summary['hidden_acceptance_regressions']['tests']} |",
            (
                "| Hidden regression rate | "
                f"{summary['hidden_acceptance_regressions']['regression_rate']} |"
            ),
            f"| Median duration ms | {summary['duration_ms']['median']} |",
            f"| P95 duration ms | {summary['duration_ms']['p95']} |",
            f"| Model calls | {summary['model_calls']['total']} |",
            f"| Successful model calls | {summary['model_calls']['successful']} |",
            f"| Token usage complete | {summary['tokens']['token_usage_complete']} |",
            f"| Total tokens | {summary['tokens']['total_tokens']} |",
            f"| Observed total tokens | {summary['tokens']['observed_total_tokens']} |",
        ]
    )
    if summary["failure_reasons"]:
        lines.extend(["", "### Failure Reasons"])
        for reason, count in summary["failure_reasons"].items():
            lines.append(f"- {reason}: {count}")

    if modes:
        lines.extend(
            [
                "",
                "## By Mode",
                "",
                (
                    "| Mode | Selected | Completed | Failed | Pending | Successful | "
                    "Selected rate | Completed rate | Median ms | P95 ms | Model calls |"
                ),
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for mode, mode_summary in modes.items():
            lines.append(
                f"| {mode} | {mode_summary['selected']} | {mode_summary['completed']} | "
                f"{mode_summary['failed']} | {mode_summary['pending']} | "
                f"{mode_summary['successful']} | {mode_summary['success_rate']['selected_rate']} | "
                f"{mode_summary['success_rate']['completed_rate']} | "
                f"{mode_summary['duration_ms']['median']} | "
                f"{mode_summary['duration_ms']['p95']} | {mode_summary['model_calls']['total']} |"
            )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate a stable RepoPilot evaluation report")
    parser.add_argument("input", type=Path, help="model-report.json or model-records directory")
    parser.add_argument("--output", type=Path, help="Write JSON output to this file")
    parser.add_argument(
        "--markdown-output", type=Path, help="Optional Markdown summary output path"
    )
    args = parser.parse_args(argv)

    report = build_eval_report(args.input)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{rendered}\n", encoding="utf-8")
    else:
        print(rendered)
    if args.markdown_output is not None:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(render_markdown(report), encoding="utf-8")


if __name__ == "__main__":
    main()
