"""Descriptive repeated-run statistics; projects are the bootstrap clusters."""

from __future__ import annotations

import itertools
import math
import random
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping

from repopilot.data_boundary import public_json, require_secret_free
from repopilot.eval_repeats import REPEAT_PROTOCOL
from repopilot.eval_report import _safe_case_summary, _summarize_records


def _success(record: Mapping) -> int:
    return int(
        record.get("status") == "completed" and bool((record.get("result") or {}).get("success"))
    )


def _measure(records: list[dict], repeats: int) -> dict:
    by_repeat = []
    for repeat in range(1, repeats + 1):
        chosen = [r for r in records if r["repeat_id"] == repeat]
        selected = len(chosen)
        by_repeat.append({
            "repeat_id": repeat, "selected": selected,
            "terminal": sum(r["status"] in {"completed", "failed"} for r in chosen),
            "successful": sum(_success(r) for r in chosen),
            "selected_rate": sum(_success(r) for r in chosen) / selected if selected else None,
        })
    rates = [r["selected_rate"] for r in by_repeat if r["selected_rate"] is not None]
    usage = [r.get("usage") or {} for r in records]
    started = [u.get("started_calls") for u in usage]
    reserved = [u.get("reserved_calls") for u in usage]
    iterations = [(r.get("result") or {}).get("iterations") for r in records]
    return {
        **_summarize_records(records),
        "unique_cases": len({r["case_id"] for r in records}),
        "unique_projects": len({r["project"] for r in records}),
        "executor_attempts": sum(int(r.get("attempts") or 0) for r in records),
        "api_requests_started": sum(started) if all(type(n) is int for n in started) else None,
        "api_reservations": sum(reserved) if all(type(n) is int for n in reserved) else None,
        "observed_api_reservations": sum(n for n in reserved if type(n) is int),
        "scheduled_transport_retries": sum(
            int(u.get("rate_limit_retries", 0)) + int(u.get("transient_retries", 0))
            for u in usage
        ),
        "coder_iterations": (
            sum(iterations) if all(type(n) is int for n in iterations) else None
        ),
        "observed_coder_iterations": sum(n for n in iterations if type(n) is int),
        "approval_reached": sum(
            (r.get("result") or {}).get("workflow_gate_passed") is True for r in records
        ),
        "initial_routes": dict(Counter(
            ((r.get("result") or {}).get("routing_history") or [{}])[0].get(
                "selected", "unreported"
            ) for r in records
        )),
        "final_routes": dict(Counter(
            ((r.get("result") or {}).get("routing") or {}).get("selected", "unreported")
            for r in records
        )),
        "route_upgrades": sum(
            int(((r.get("result") or {}).get("routing") or {}).get("upgrades", 0))
            for r in records
        ),
        "model_review_performed": sum(
            (r.get("result") or {}).get("review_performed") is True for r in records
        ),
        "pending_by_status": dict(Counter(
            r["status"] for r in records if r["status"] not in {"completed", "failed"}
        )),
        "repeats": by_repeat,
        "repeat_rate_range": [min(rates), max(rates)] if rates else None,
        "complete": all(r["status"] in {"completed", "failed"} for r in records),
    }


def _cluster_interval(projects: dict[str, list[float]]) -> dict:
    groups = list(projects.values())
    count = len(groups)
    if count < 2:
        return {"interval": None, "projects": count, "degenerate": None}
    if count <= 6:
        draws = itertools.product(range(count), repeat=count)
        method = "exact-project-cluster-resampling"
    else:
        rng = random.Random(7)  # noqa: S311 -- deterministic statistics only
        draws = ([rng.randrange(count) for _ in groups] for _ in range(5000))
        method = "5000-project-cluster-resamples-seed-7"
    values = sorted(statistics.mean(
        delta for index in draw for delta in groups[index]
    ) for draw in draws)
    low = values[math.floor((len(values) - 1) * 0.025)]
    high = values[math.ceil((len(values) - 1) * 0.975)]
    return {
        "interval": [low, high], "projects": count, "method": method,
        "level": 0.95, "quantiles": "outward-order-statistics",
        "degenerate": low == high,
    }


def build_repeated_report(payload: Mapping) -> dict:
    metadata = payload["metadata"]
    require_secret_free(metadata)
    if metadata.get("repeat_protocol") != REPEAT_PROTOCOL:
        raise ValueError("Unsupported repeat report protocol")
    config = metadata["config"]
    repeats, modes = config["repeats"], config["base"]["modes"]
    cases = metadata["selected_cases"]
    ids = [case["id"] for case in cases]
    if len(set(ids)) != len(ids) or not ids or type(repeats) is not int or repeats < 1:
        raise ValueError("Invalid repeated report selection")
    expected = {(case_id, repeat, mode) for case_id in ids
                for repeat in range(1, repeats + 1) for mode in modes}
    records = {}
    for record in payload["records"]:
        key = (record["case_id"], record["repeat_id"], record["mode"])
        if key not in expected or key in records:
            raise ValueError("Duplicate or unselected repeated result")
        if record.get("run_id") != config["run_id"]:
            raise ValueError("Mixed repeated run identities")
        if (
            record.get("strategy_version") != config["strategy_version"]
            or record.get("cohort_fingerprint") != metadata["fingerprint"]
        ):
            raise ValueError("Mixed repeated strategy/cohort identities")
        records[key] = dict(record)
    by_id = {case["id"]: case for case in cases}
    for key in expected - records.keys():
        case_id, repeat, mode = key
        records[key] = {
            "case_id": case_id, "repeat_id": repeat, "mode": mode,
            "run_id": config["run_id"], "status": "pending_missing",
            "attempts": 0, "usage": {}, "result": None,
        }
    values = []
    for key in sorted(records):
        record = records[key]
        case = by_id[key[0]]
        record.update(project=case.get("project", "unlabeled"),
                      difficulty=case.get("difficulty", "unlabeled"))
        values.append(record)
    paired = []
    for first, second in itertools.combinations(modes, 2):
        deltas: dict[str, list[float]] = defaultdict(list)
        eligible = 0
        for case in cases:
            chosen = [records[(case["id"], repeat, mode)]
                      for repeat in range(1, repeats + 1) for mode in (first, second)]
            if any(r["status"] not in {"completed", "failed"} for r in chosen):
                continue
            delta = sum(_success(records[(case["id"], repeat, second)])
                        - _success(records[(case["id"], repeat, first)])
                        for repeat in range(1, repeats + 1)) / repeats
            deltas[case.get("project", "unlabeled")].append(delta)
            eligible += 1
        flat = [d for group in deltas.values() for d in group]
        paired.append({
            "first": first, "second": second, "direction": "second-minus-first",
            "selected_unique_cases": len(cases), "eligible_unique_cases": eligible,
            "excluded_incomplete_cases": len(cases) - eligible,
            "mean_case_rate_difference": statistics.mean(flat) if flat else None,
            "project_cluster_interval": _cluster_interval(deltas),
            "complete": eligible == len(cases),
        })
    strata = {}
    for field in ("project", "difficulty"):
        strata[field] = {
            label: {mode: _measure([r for r in values if r[field] == label and r["mode"] == mode],
                                  repeats) for mode in modes}
            for label in sorted({r[field] for r in values})
        }
    mode_reports = {mode: _measure([r for r in values if r["mode"] == mode], repeats)
                    for mode in modes}
    overall = _summarize_records(values)
    budget = metadata.get("budget", {})
    unknown_reports = [overall] if budget.get("orphan_reservations", 0) else []
    unknown_reports += [mode_reports[mode] for mode in modes
                        if budget.get("orphan_by_mode", {}).get(mode, 0)]
    if budget.get("orphan_reservations", 0):
        unknown_reports.extend(item for groups in strata.values()
                               for group in groups.values() for item in group.values())
    for item in unknown_reports:
        item["tokens"].update(
            token_usage_complete=False, input_tokens=None, output_tokens=None, total_tokens=None,
        )
    return public_json({
        "schema_version": 3, "metadata": metadata,
        "summary": overall, "modes": mode_reports,
        "repeat_statistics": {
            "protocol": REPEAT_PROTOCOL, "selected_unique_cases": len(cases),
            "selected_projects": len({r["project"] for r in values}),
            "selected_units": len(expected), "repeats": repeats,
            "paired": paired, "strata": strata,
            "assumptions": "Case success fractions average predeclared repeats. Paired intervals "
            "resample whole projects, not individual repeated outcomes. The curated sample is "
            "not random; intervals are descriptive, small cluster counts are unstable, and "
            "constant observed outcomes yield degenerate intervals. Pending results do not "
            "establish a strategy winner. No seed was sent to providers; repeats may vary.",
            "money": None,
        },
        "cases": [{**_safe_case_summary(r), "run_id": r["run_id"],
                   "repeat_id": r["repeat_id"], "project": r["project"],
                   "difficulty": r["difficulty"]} for r in values],
    })
