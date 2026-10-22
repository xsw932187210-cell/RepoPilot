import asyncio
import fcntl
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from repopilot.eval_repeats import RepeatConfig, RepeatedEvaluationRuntime
from repopilot.eval_report import build_eval_report
from repopilot.eval_runtime import EvaluationConfig, ResumeMismatchError


def config(**updates):
    base = EvaluationConfig(
        dataset_sha256="a" * 64, provider="mock", model="fixture", temperature=0,
        max_model_calls=2, modes=("oneshot", "workflow"),
    )
    return replace(RepeatConfig(base, "pilot", 2, "v1", 8), **updates)


CASES = [{"id": "a", "project": "first"}, {"id": "b", "project": "second"}]


async def response():
    return {"usage_metadata": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}


@pytest.mark.asyncio
async def test_repeats_rotate_order_separate_paths_and_resume_without_new_calls(tmp_path):
    visits = []

    async def execute(case, mode, budget):
        visits.append((case["id"], mode))
        await budget.call(response, call_role="coder")
        return {"success": True, "iterations": 1, "workflow_gate_passed": mode == "workflow"}

    runtime = RepeatedEvaluationRuntime(config(), tmp_path)
    report = await runtime.run(CASES, execute)
    assert visits == [
        ("a", "oneshot"), ("a", "workflow"), ("b", "workflow"), ("b", "oneshot"),
        ("a", "workflow"), ("a", "oneshot"), ("b", "oneshot"), ("b", "workflow"),
    ]
    assert len(list((tmp_path / "runs").glob("**/cases/*.json"))) == 8
    assert report["metadata"]["budget"]["reserved_api_attempts"] == 8
    resumed = await RepeatedEvaluationRuntime(config(), tmp_path).run(CASES, execute)
    assert len(visits) == 8
    assert resumed == report
    aggregate = build_eval_report(report)
    assert aggregate["schema_version"] == 3
    assert aggregate["repeat_statistics"]["selected_unique_cases"] == 2
    assert aggregate["repeat_statistics"]["selected_units"] == 8
    assert aggregate["modes"]["oneshot"]["executor_attempts"] == 4
    assert aggregate["modes"]["oneshot"]["api_requests_started"] == 4


@pytest.mark.asyncio
async def test_total_ceiling_counts_transport_retries_and_keeps_unstarted_pending(tmp_path):
    calls = 0

    class Unavailable(RuntimeError):
        status_code = 503

    async def invoke():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Unavailable("synthetic failure")
        return await response()

    async def execute(case, mode, budget):
        await budget.call(invoke)
        return {"success": True}

    runtime = RepeatedEvaluationRuntime(
        config(max_api_attempts=2), tmp_path, base_backoff_seconds=0,
    )
    result = await runtime.run(CASES, execute)
    assert calls == 2
    assert result["metadata"]["budget"]["reserved_transport_retries"] == 1
    assert sum(r["status"] == "completed" for r in result["records"]) == 1
    assert sum(r["status"] == "pending_budget" for r in result["records"]) == 7
    assert all(r["attempts"] == 0 for r in result["records"][1:])
    assert build_eval_report(result)["summary"]["tokens"]["total_tokens"] is None
    await RepeatedEvaluationRuntime(
        config(max_api_attempts=2), tmp_path, base_backoff_seconds=0,
    ).run(CASES, execute)


@pytest.mark.asyncio
async def test_started_request_crash_consumes_shared_and_unit_budget(tmp_path):
    class Crash(BaseException):
        pass

    async def interrupted():
        raise Crash

    async def execute(case, mode, budget):
        await budget.call(interrupted)

    with pytest.raises(Crash):
        await RepeatedEvaluationRuntime(config(), tmp_path).run(CASES, execute)
    ledger = json.loads((tmp_path / "api-reservations.json").read_text())
    assert len(ledger["reservations"]) == 1

    async def resumed(case, mode, budget):
        await budget.call(response)
        return {"success": True}

    result = await RepeatedEvaluationRuntime(config(), tmp_path).run(CASES, resumed)
    assert result["metadata"]["budget"]["reserved_api_attempts"] == 8
    first = result["records"][0]
    assert first["usage"]["reserved_calls"] == 2
    assert first["usage"]["unknown_calls"] == 1
    assert first["attempts"] == 2
    assert result["records"][-1]["status"] == "pending_budget"


@pytest.mark.asyncio
async def test_shared_reserve_survives_crash_before_local_save(tmp_path, monkeypatch):
    class Crash(BaseException):
        pass

    runtime = RepeatedEvaluationRuntime(config(), tmp_path)
    reserve = runtime._reserve

    async def interrupted(unit, payload):
        await reserve(unit, payload)
        raise Crash

    monkeypatch.setattr(runtime, "_reserve", interrupted)
    sent = 0

    async def invoke():
        nonlocal sent
        sent += 1
        return await response()

    async def execute(case, mode, budget):
        await budget.call(invoke)
        return {"success": True}

    with pytest.raises(Crash):
        await runtime.run(CASES, execute)
    assert sent == 0
    result = await RepeatedEvaluationRuntime(config(), tmp_path).run(CASES, execute)
    assert sent == 7
    assert result["metadata"]["budget"]["reserved_api_attempts"] == 8
    assert result["metadata"]["budget"]["orphan_reservations"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"run_id": "other"}, {"repeats": 1}, {"strategy_version": "v2"},
    {"max_api_attempts": 9},
])
async def test_repeat_identity_changes_refuse_resume(tmp_path, change):
    async def execute(case, mode, budget):
        return {"success": True}

    await RepeatedEvaluationRuntime(config(), tmp_path).run(CASES, execute)
    with pytest.raises(ResumeMismatchError):
        await RepeatedEvaluationRuntime(config(**change), tmp_path).run(CASES, execute)


@pytest.mark.asyncio
async def test_missing_ledger_or_live_owner_refuses_a_second_runner(tmp_path):
    async def execute(case, mode, budget):
        await budget.call(response)
        return {"success": True}

    await RepeatedEvaluationRuntime(config(), tmp_path).run(CASES, execute)
    with (tmp_path / ".run.lock").open() as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            await RepeatedEvaluationRuntime(config(), tmp_path).run(CASES, execute)
    (tmp_path / "api-reservations.json").unlink()
    with pytest.raises(ResumeMismatchError, match="Missing reservation"):
        await RepeatedEvaluationRuntime(config(), tmp_path).run(CASES, execute)


@pytest.mark.asyncio
async def test_ledger_truncation_cannot_restore_spent_budget(tmp_path):
    async def execute(case, mode, budget):
        await budget.call(response)
        return {"success": True}

    await RepeatedEvaluationRuntime(config(), tmp_path).run(CASES, execute)
    ledger = tmp_path / "api-reservations.json"
    raw = json.loads(ledger.read_text())
    raw["reservations"].pop()
    ledger.write_text(json.dumps(raw))
    with pytest.raises(ResumeMismatchError, match="lost durable"):
        await RepeatedEvaluationRuntime(config(), tmp_path).run(CASES, execute)


@pytest.mark.asyncio
async def test_actual_process_exit_retains_global_and_started_reservations(tmp_path):
    script = tmp_path / "crash.py"
    records = tmp_path / "records"
    script.write_text('''
import asyncio, os, sys
from pathlib import Path
from repopilot.eval_repeats import RepeatConfig, RepeatedEvaluationRuntime
from repopilot.eval_runtime import EvaluationConfig
async def main():
    base = EvaluationConfig(dataset_sha256="a"*64, provider="mock", model="fixture",
                            temperature=0, max_model_calls=2, modes=("workflow",))
    config = RepeatConfig(base, "hard-exit", 1, "v1", 4)
    async def invoke():
        os._exit(91)
    async def execute(case, mode, budget):
        await budget.call(invoke)
    await RepeatedEvaluationRuntime(config, Path(sys.argv[1])).run(
        [{"id":"a","project":"first"},{"id":"b","project":"second"}], execute)
asyncio.run(main())
''')
    child = await asyncio.to_thread(
        subprocess.run,  # owned deterministic crash probe, no upstream code
        [sys.executable, str(script), str(records)], capture_output=True,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        check=False,
    )
    assert child.returncode == 91, child.stderr.decode()
    base = replace(config().base, modes=("workflow",))
    selected = RepeatConfig(base, "hard-exit", 1, "v1", 4)
    requests = 0

    async def execute(case, mode, budget):
        nonlocal requests
        requests += 1
        await budget.call(response)
        return {"success": True}

    result = await RepeatedEvaluationRuntime(selected, records).run(CASES, execute)
    assert requests == 2
    assert result["metadata"]["budget"]["reserved_api_attempts"] == 3
    assert result["records"][0]["usage"]["unknown_calls"] == 1
    assert result["records"][0]["cumulative_duration_ms"] is None
    await RepeatedEvaluationRuntime(selected, records).run(CASES, execute)
    assert requests == 2


@pytest.mark.asyncio
async def test_executor_cannot_mutate_frozen_case_identity(tmp_path):
    original = [{"id": "a", "project": "first"}]

    async def execute(case, mode, budget):
        case["id"] = "mutated"
        return {"success": True}

    result = await RepeatedEvaluationRuntime(config(), tmp_path).run(original, execute)
    assert original[0]["id"] == "a"
    assert {r["case_id"] for r in result["records"]} == {"a"}
