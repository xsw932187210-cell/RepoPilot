from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from approval_helpers import approval_case, decision_request
from sqlalchemy import select

from repopilot.api import app
from repopilot.approval_models import evidence_identity
from repopilot.approvals import ApprovalConflict, ApprovalStore, DecisionRecord, EvidenceRecord
from repopilot.candidates import ArtifactStore, candidate_identity
from repopilot.db import Database
from repopilot.models import TaskStatus


@pytest.fixture
async def database(tmp_path: Path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'approval.db'}")
    await database.setup()
    try:
        yield database
    finally:
        await database.close()


async def test_decision_is_idempotent_and_conflicting_repeat_is_rejected(database, tmp_path):
    _, view = await approval_case(database, tmp_path / "workspaces")
    store = ApprovalStore(database)
    request = decision_request(view)
    first = await store.decide(view.task_id, request, actor="operator")
    assert await store.decide(view.task_id, request, actor="operator") == first
    with pytest.raises(ApprovalConflict, match="already_decided"):
        await store.decide(view.task_id, decision_request(view, approved=False), actor="operator")
    current = await database.get_task(view.task_id)
    assert current.status == TaskStatus.QUEUED
    assert current.state_version == view.request_version + 1
    assert (await store.current(view.task_id)).decision.decision_id == first.decision_id
    async with database.sessions() as session:
        assert len((await session.execute(select(DecisionRecord))).scalars().all()) == 1


@pytest.mark.parametrize("field", ["candidate_id", "evidence_digest", "request_version"])
async def test_stale_approval_cannot_approve_newest_candidate(database, tmp_path, field):
    _, view = await approval_case(database, tmp_path / "workspaces")
    request = decision_request(view)
    wrong = request.model_copy(update={field: 999 if field == "request_version" else "0" * 64})
    with pytest.raises(ApprovalConflict, match="stale_approval_binding"):
        await ApprovalStore(database).decide(view.task_id, wrong, actor="operator")
    assert (await database.get_task(view.task_id)).status == TaskStatus.AWAITING_APPROVAL


@pytest.mark.parametrize("state", ["expired", "cancelled", "non-waiting"])
async def test_stopped_or_expired_approval_is_rejected(database, tmp_path, state):
    _, view = await approval_case(
        database,
        tmp_path / "workspaces",
        ttl_seconds=-1 if state == "expired" else 100,
    )
    if state == "cancelled":
        await database.transition_task(
            view.task_id,
            expected_status=TaskStatus.AWAITING_APPROVAL,
            expected_version=view.request_version,
            status=TaskStatus.CANCELLED,
        )
    elif state == "non-waiting":
        await database.transition_task(
            view.task_id,
            expected_status=TaskStatus.AWAITING_APPROVAL,
            expected_version=view.request_version,
            status=TaskStatus.QUEUED,
        )
    store = ApprovalStore(database)
    with pytest.raises(
        ApprovalConflict,
        match={
            "expired": "approval_expired",
            "cancelled": "approval_cancelled",
            "non-waiting": "task_not_awaiting_approval",
        }[state],
    ):
        await store.decide(view.task_id, decision_request(view), actor="operator")


async def test_redis_failure_is_saved_and_explicit_retry_survives_database_reopen(
    database,
    tmp_path,
    monkeypatch,
):
    settings, view = await approval_case(database, tmp_path / "workspaces")
    queue = SimpleNamespace(enqueue=AsyncMock(side_effect=ConnectionError("unavailable")))
    for key, value in {
        "settings": settings,
        "database": database,
        "queue": queue,
        "events": None,
        "redis": None,
    }.items():
        monkeypatch.setattr(app.state, key, value, raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        legacy = await client.post(
            f"/api/v1/tasks/{view.task_id}/approval", json={"approved": True}
        )
        assert legacy.status_code == 422
        response = await client.post(
            f"/api/v1/tasks/{view.task_id}/approval",
            json=decision_request(view).model_dump(mode="json"),
        )
        assert response.status_code == 202
        receipt = response.json()
        assert receipt["saved"] and receipt["decision"]["dispatch_state"] == "pending"
        assert receipt["decision"]["dispatch_error"] == "queue_delivery_failed"
        await database.close()
        reopened = Database(database.url)
        monkeypatch.setattr(app.state, "database", reopened)
        try:
            restored = await client.get(f"/api/v1/tasks/{view.task_id}/approval")
            assert restored.json()["candidate"] == view.candidate.model_dump(mode="json")
            assert restored.json()["evidence"] == view.evidence.model_dump(mode="json")
            queue.enqueue.side_effect = None
            retry = await client.post(
                f"/api/v1/tasks/{view.task_id}/approval/dispatch",
                json={"decision_id": receipt["decision"]["decision_id"]},
            )
            assert retry.status_code == 200
            assert retry.json()["decision"]["dispatch_state"] == "enqueued"
            assert queue.enqueue.await_count == 2
            current = await reopened.get_task(view.task_id)
            running = await reopened.transition_task(
                view.task_id,
                expected_status=current.status,
                expected_version=current.state_version,
                status=TaskStatus.RUNNING,
            )
            store = ApprovalStore(reopened)
            resume = await store.resume_for_running_task(running)
            assert (await store.validate_resume(view.task_id, resume)).approved is True
            with pytest.raises(ApprovalConflict, match="already_consumed"):
                await store.resume_for_running_task(running)
            stale = {**resume, "approved": False}
            with pytest.raises(ApprovalConflict, match="stale_approval_resume"):
                await store.validate_resume(view.task_id, stale)
        finally:
            await reopened.close()


async def test_artifact_endpoints_are_controlled_and_complete(database, tmp_path, monkeypatch):
    settings, view = await approval_case(database, tmp_path / "workspaces")
    settings.api_key = "unit-test-key"
    for key, value in {
        "settings": settings,
        "database": database,
        "queue": None,
        "events": None,
        "redis": None,
    }.items():
        monkeypatch.setattr(app.state, key, value, raising=False)
    url = f"/api/v1/tasks/{view.task_id}/candidates/{view.candidate.candidate_id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get(url)).status_code == 401
        headers = {"X-API-Key": settings.api_key}
        assert (await client.get(url, headers=headers)).json() == view.candidate.model_dump(
            mode="json"
        )
        diff = await client.get(url + "/diff", headers=headers)
        assert diff.status_code == 200 and "return a + b" in diff.text
        other = await client.get(url.replace(view.task_id, "other-task"), headers=headers)
        assert other.status_code == 404
        (
            settings.workspace_root
            / ".artifacts"
            / view.candidate.artifact_digest
            / "manifest.json"
        ).write_bytes(b"invalid JSON")
        response = await client.post(
            f"/api/v1/tasks/{view.task_id}/approval",
            headers=headers,
            json=decision_request(view).model_dump(mode="json"),
        )
        assert response.status_code == 503
        assert (await ApprovalStore(database).current(view.task_id)).decision is None


async def test_pending_decision_is_blocked_by_cancellation(database, tmp_path):
    _, view = await approval_case(database, tmp_path / "workspaces")
    store = ApprovalStore(database)
    decision = await store.decide(view.task_id, decision_request(view), actor="operator")
    current = await database.get_task(view.task_id)
    await database.transition_task(
        view.task_id,
        expected_status=current.status,
        expected_version=current.state_version,
        status=TaskStatus.CANCELLED,
    )
    assert (await store.decision(view.task_id, decision.decision_id)).dispatch_state == "blocked"
    with pytest.raises(ApprovalConflict, match="not_dispatchable"):
        await store.dispatchable(view.task_id, decision.decision_id)


async def test_old_request_cannot_approve_a_later_generation(database, tmp_path):
    settings, view = await approval_case(database, tmp_path / "workspaces")
    store = ApprovalStore(database)
    current = await database.transition_task(
        view.task_id,
        expected_status=TaskStatus.AWAITING_APPROVAL,
        expected_version=view.request_version,
        status=TaskStatus.QUEUED,
    )
    running = await database.transition_task(
        view.task_id,
        expected_status=current.status,
        expected_version=current.state_version,
        status=TaskStatus.RUNNING,
    )
    source = settings.workspace_root / view.task_id / "calculator.py"
    source.write_text(source.read_text() + "\n# later generation\n")
    candidate = await ArtifactStore(settings.workspace_root / ".artifacts").freeze(
        source.parent,
        task_id=view.task_id,
        base_sha=view.candidate.base_sha,
        generation=2,
        policy_version=view.candidate.policy_version,
    )
    evidence = view.evidence.model_copy(
        update={
            "candidate_id": candidate.candidate_id,
            "artifact_digest": candidate.artifact_digest,
        }
    )
    evidence = evidence.model_copy(update={"evidence_digest": evidence_identity(evidence)})
    await store.open_request(
        view.task_id,
        expected_version=running.state_version,
        candidate=candidate,
        evidence=evidence,
        summary={"review": {"risk_level": "low"}},
    )
    with pytest.raises(ApprovalConflict, match="approval_superseded"):
        await store.decide(view.task_id, decision_request(view), actor="operator")
    latest = await store.current(view.task_id)
    assert latest.candidate.generation == 2 and latest.status == "waiting"


async def test_failed_test_evidence_is_persisted_without_an_approval(database, tmp_path):
    _, view = await approval_case(database, tmp_path / "workspaces")
    store = ApprovalStore(database)
    candidate = view.candidate.model_copy(update={"generation": 2})
    candidate = candidate.model_copy(update={"candidate_id": candidate_identity(candidate)})
    evidence = view.evidence.model_copy(
        update={
            "candidate_id": candidate.candidate_id,
            "result": view.evidence.result.model_copy(update={"exit_code": 1}),
        }
    )
    evidence = evidence.model_copy(update={"evidence_digest": evidence_identity(evidence)})
    await store.record_evidence(view.task_id, candidate, evidence)
    await store.record_evidence(view.task_id, candidate, evidence)
    async with database.sessions() as session:
        row = await session.get(EvidenceRecord, evidence.evidence_digest)
        assert row.payload["result"]["exit_code"] == 1
    with pytest.raises(ApprovalConflict, match="invalid_candidate_evidence_binding"):
        await store.open_request(
            view.task_id,
            expected_version=view.request_version,
            candidate=candidate,
            evidence=evidence,
            summary={},
        )
    assert (await store.current(view.task_id)).approval_id == view.approval_id
