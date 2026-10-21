from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.engine import make_url

from repopilot.candidates import ArtifactStore, Candidate
from repopilot.db import Database
from repopilot.models import TaskCreate, TaskStatus

POSTGRES_URL = os.getenv("REPOPILOT_CH02B_POSTGRES_TEST_URL", "")
API_URL = os.getenv("REPOPILOT_CH02B_API_URL", "")
LEGACY_SOURCE = os.getenv("REPOPILOT_CH02B_LEGACY_SOURCE", "")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="run scripts/ch02b_integration.sh for the isolated CH-02B service probe",
)


async def child(phase: str, scenario: str, task_id: str) -> int:
    environment = dict(os.environ)
    if phase == "legacy":
        assert await asyncio.to_thread(Path(LEGACY_SOURCE, "repopilot", "worker.py").is_file)
        environment["PYTHONPATH"] = LEGACY_SOURCE
    helper = Path(__file__).parent / "probes" / "ch02b_process.py"
    # Fixed interpreter/helper paths and test-owned task IDs; never use a shell.
    result = await asyncio.to_thread(
        subprocess.run,  # noqa: S603
        [sys.executable, str(helper), phase, scenario, task_id],
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode in {0, 86}, result.stderr
    return result.returncode


async def database() -> Database:
    assert make_url(POSTGRES_URL).database in {
        "repopilot_ch02b_test", "repopilot_ch03a_test", "repopilot_ch05a_test",
    }, (
        "The CH-02B probe refuses non-dedicated databases"
    )
    db = Database(POSTGRES_URL)
    await db.setup()
    return db


async def task(db: Database):
    return await db.create_task(
        TaskCreate(
            repository_url="demo://buggy-calculator",
            issue_title="Fix add returning the wrong result",
            issue_body="The add(a, b) function subtracts b instead of adding it.",
            test_command="python -m pytest -q",
        )
    )


async def snapshot(task_row):
    url = POSTGRES_URL.replace("postgresql+asyncpg://", "postgresql://")
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        checkpoint = await saver.aget_tuple(
            {"configurable": {"thread_id": task_row.graph_thread_id}}
        )
        assert checkpoint is not None
        return checkpoint.checkpoint["channel_values"]


async def approve_and_wait(task_id: str) -> dict:
    assert API_URL
    async with httpx.AsyncClient(base_url=API_URL, timeout=5, trust_env=False) as client:
        view = (await client.get(f"/api/v1/tasks/{task_id}/approval")).json()
        response = await client.post(
            f"/api/v1/tasks/{task_id}/approval",
            json={
                "schema_version": "approval-v2",
                "approval_id": view["approval_id"],
                "request_version": view["request_version"],
                "candidate_id": view["candidate"]["candidate_id"],
                "evidence_digest": view["evidence"]["evidence_digest"],
                "approved": True,
                "feedback": "isolated CH-02B probe",
            },
        )
        assert response.status_code == 200
        for _ in range(60):
            response = await client.get(f"/api/v1/tasks/{task_id}")
            response.raise_for_status()
            result = response.json()
            if result["status"] == "completed":
                return result
            assert result["status"] not in {"failed", "cancelled"}
            await asyncio.sleep(0.25)
    pytest.fail("approval did not complete the original task")


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["once", "repeat", "budget", "stale", "redaction"])
async def test_postgres_policy_correction_survives_process_exit(scenario: str) -> None:
    db = await database()
    try:
        row = await task(db)
        assert await child("pause", scenario, row.id) == 86
        paused = await snapshot(row)
        assert paused["policy_retry_count"] == 1
        assert paused["policy_feedback"]["reason_code"] == (
            "outside_context" if scenario == "redaction" else "write_denied"
        )
        workspace = Path(paused["workspace"])
        source = workspace / "calculator.py"
        tests = workspace / "tests" / "test_calculator.py"
        original_tests = tests.read_bytes()
        assert "return a - b" in source.read_text()
        assert "forbidden CH-02B" not in tests.read_text()
        before = await db.get_model_call_metrics(row.id)
        assert before is not None and before["reserved_calls"] == 2
        if scenario == "stale":
            source.write_text(source.read_text() + "\n# external CH-02B probe update\n")
        assert await child("resume", scenario, row.id) == 0
        recovered = await db.get_task(row.id)
        assert recovered is not None and recovered.result is not None
        assert recovered.graph_thread_id == row.graph_thread_id
        assert recovered.result["policy_retry_count"] == 1
        after = await db.get_model_call_metrics(row.id)
        assert after is not None
        assert (
            after["reserved_calls"]
            == {"once": 4, "redaction": 4, "repeat": 3, "stale": 3, "budget": 2}[scenario]
        )
        assert after["successful_model_calls"] == after["reserved_calls"]
        attempts = await db.list_model_call_attempts(row.id)
        assert len(attempts) == after["reserved_calls"]
        assert tests.read_bytes() == original_tests
        if scenario in {"once", "redaction"}:
            assert recovered.status is TaskStatus.AWAITING_APPROVAL
            current_state = await snapshot(row)
            candidate = Candidate.model_validate(current_state["candidate"])
            with ArtifactStore(workspace.parent / ".artifacts").materialize(candidate) as tree:
                assert "return a + b" in (tree / "calculator.py").read_text()
            assert "return a - b" in source.read_text()
            assert recovered.result["test_result"]["exit_code"] == 0
            assert recovered.result["test_result"]["timed_out"] is False
            events = await db.list_events(row.id)
            denials = [
                event
                for event in events
                if event.node == "apply_policy" and event.payload.get("feedback")
            ]
            assert len(denials) == 1
            assert "sk-fixture-private-marker" not in json.dumps(recovered.result)
            completed = await approve_and_wait(row.id)
            assert completed["result"]["policy_retry_count"] == 1
            final_metrics = await db.get_model_call_metrics(row.id)
            assert final_metrics is not None and final_metrics["reserved_calls"] == 4
        else:
            assert recovered.status is TaskStatus.FAILED
            assert recovered.error == (
                "model_call_budget_exhausted" if scenario == "budget" else "policy_retry_exhausted"
            )
            if scenario == "stale":
                assert recovered.result["policy_feedback"]["reason_code"] == "stale_content"
                assert "external CH-02B probe update" in source.read_text()
            assert "return a - b" in source.read_text()
        print(f"CH-02B scenario={scenario} policy_retry_count=1 calls={after['reserved_calls']}")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_pre_ch03a_unbound_checkpoint_requires_revalidation() -> None:
    db = await database()
    try:
        row = await task(db)
        assert await child("legacy", "once", row.id) == 0
        old_state = await snapshot(row)
        assert old_state["policy_retry_count"] == 0
        assert "policy_feedback" not in old_state
        original_tests = (
            Path(old_state["workspace"]) / "tests" / "test_calculator.py"
        ).read_bytes()
        before = await db.get_model_call_metrics(row.id)
        assert before is not None and before["reserved_calls"] == 3
        async with httpx.AsyncClient(base_url=API_URL, trust_env=False) as client:
            response = await client.get(f"/api/v1/tasks/{row.id}/approval")
            assert response.status_code == 409
            assert response.json()["detail"] == "legacy_approval_requires_revalidation"
            response = await client.post(
                f"/api/v1/tasks/{row.id}/approval",
                json={"approved": True},
            )
            assert response.status_code == 422
        current = await db.get_task(row.id)
        assert current is not None and current.graph_thread_id == row.graph_thread_id
        assert current.status is TaskStatus.AWAITING_APPROVAL
        after = await db.get_model_call_metrics(row.id)
        assert after is not None and after["reserved_calls"] == 3
        nodes = current.result["node_metrics"]
        assert sum(item["node"] == "test_runner" for item in nodes) == 1
        assert sum(item["node"] == "finalize" for item in nodes) == 0
        assert (
            Path(old_state["workspace"]) / "tests" / "test_calculator.py"
        ).read_bytes() == original_tests
        print("CH-03A legacy_checkpoint=12b9e3d requires_revalidation=passed calls=3")
    finally:
        await db.close()
