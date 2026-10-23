"""Exercise persisted strategies through actual isolated API/Worker/Docker services."""

import asyncio
import os

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("REPOPILOT_CH01B_DOCKER_TEST") != "1",
    reason="requires the dedicated CH-01B Docker service harness",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy,expected_calls", [("oneshot", 1), ("auto", 1), ("workflow", 3)])
async def test_routes_persist_and_require_approval_in_real_worker(strategy, expected_calls):
    async with httpx.AsyncClient(
        base_url=os.environ["REPOPILOT_CH06C_API_URL"], timeout=20,
    ) as client:
        response = await client.post("/api/v1/tasks", json={
            "repository_url": "demo://buggy-calculator",
            "issue_title": "Fix add returning the wrong result",
            "issue_body": "The add function subtracts instead of adding its two arguments.",
            "execution_strategy": strategy,
        })
        assert response.status_code == 202, response.text
        task = response.json()
        assert task["execution_strategy"] == strategy
        task_id = task["id"]
        for _ in range(300):
            task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
            if task["status"] != "queued" and task["status"] != "running":
                break
            await asyncio.sleep(0.1)
        assert task["status"] == "awaiting_approval", task
        assert task["result"]["pull_request_url"] is None
        routing = task["result"]["routing"]
        assert routing["requested"] == strategy
        assert routing["selected"] == ("workflow" if strategy == "workflow" else "oneshot")
        assert task["result"]["review_performed"] is (strategy == "workflow")
        metrics = (await client.get(f"/api/v1/tasks/{task_id}/metrics")).json()
        assert metrics["model_calls_started"] == expected_calls
        assert metrics["selected_route"] == routing["selected"]
        approval = (await client.get(f"/api/v1/tasks/{task_id}/approval")).json()
        response = await client.post(f"/api/v1/tasks/{task_id}/approval", json={
            "schema_version": "approval-v2", "approval_id": approval["approval_id"],
            "request_version": approval["request_version"],
            "candidate_id": approval["candidate"]["candidate_id"],
            "evidence_digest": approval["evidence"]["evidence_digest"], "approved": False,
            "feedback": "Routing integration rejects publication",
        })
        assert response.status_code in (200, 202), response.text
        for _ in range(300):
            task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
            if task["status"] in {"cancelled", "completed", "failed"}:
                break
            await asyncio.sleep(0.1)
        assert task["status"] == "cancelled", task
        assert task["result"]["pull_request_url"] is None
        assert (await client.get(f"/api/v1/tasks/{task_id}/metrics")).json()[
            "model_calls_started"
        ] == expected_calls
