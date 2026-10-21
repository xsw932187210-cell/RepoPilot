from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from approval_helpers import decision_request
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from publication_helpers import GitHubServer, publication_case
from sqlalchemy import select

from repopilot.api import app
from repopilot.approval_models import TestEvidence as Evidence
from repopilot.approvals import ApprovalConflict, ApprovalStore
from repopilot.candidates import ArtifactStore, _git
from repopilot.config import Settings
from repopilot.db import Database
from repopilot.github import GitHubPublisher
from repopilot.graph import GraphDependencies, build_graph
from repopilot.llm import build_agent_model
from repopilot.migrations import (
    HEAD_REVISION,
    current_database_revisions,
    ensure_database_schema,
)
from repopilot.models import TaskCreate, TaskStatus
from repopilot.publication import (
    PublicationConflict,
    PublishIntent,
    PublishIntentRecord,
    artifact_objects,
    commit_oid,
    git_tree,
)
from repopilot.repository import WorkspaceManager
from repopilot.sandbox import LocalSandbox
from repopilot.worker import result_summary


@pytest.fixture
async def database(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'publication.db'}")
    await database.setup()
    try:
        yield database
    finally:
        await database.close()


async def test_native_git_confirms_full_binary_executable_tree_and_commit_identity(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    (root / "a").mkdir()
    (root / "a" / "nested.py").write_bytes(b"#!/usr/bin/python\r\nprint('hi')\r\n")
    (root / "a" / "nested.py").chmod(0o755)
    (root / "a.c").write_bytes(b"\x00\xff\x01")
    (root / "中文.txt").write_text("完整快照\n")
    _git(root, "init", "-q")
    _git(root, "add", "--all", "--force")
    _git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-qm",
        "base",
    )
    base = _git(root, "rev-parse", "HEAD").decode().strip()
    (root / "a.c").write_bytes(b"\xff\x00\n")
    (root / "empty-directory").mkdir()
    artifacts = ArtifactStore(tmp_path / "artifacts")
    candidate = await artifacts.freeze(
        root,
        task_id="native-git",
        base_sha=base,
        generation=1,
        policy_version="fixture",
    )
    entries, _ = artifact_objects(artifacts, candidate)
    _git(root, "add", "--all", "--force")
    tree = _git(root, "write-tree").decode().strip()
    assert git_tree(entries) == tree
    date = datetime(2026, 10, 22, 1, 2, 3, tzinfo=UTC)
    intent = PublishIntent(
        publish_intent_id="a" * 64,
        task_id="native-git",
        task_version=5,
        decision_id="decision",
        candidate_id=candidate.candidate_id,
        evidence_digest="b" * 64,
        target_repo="example/project",
        base_branch="main",
        approved_base_sha=base,
        branch="fixture",
        title="Fixture",
        commit_date=date,
        tree_sha=tree,
        commit_sha="",
    )
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "RepoPilot",
        "GIT_COMMITTER_NAME": "RepoPilot",
        "GIT_AUTHOR_EMAIL": "repopilot@users.noreply.github.com",
        "GIT_COMMITTER_EMAIL": "repopilot@users.noreply.github.com",
        "GIT_AUTHOR_DATE": date.isoformat(),
        "GIT_COMMITTER_DATE": date.isoformat(),
    }
    result = await asyncio.to_thread(
        subprocess.run,
        ["git", "commit-tree", tree, "-p", base],  # noqa: S607
        cwd=root,
        input=intent.message.encode(),
        capture_output=True,
        check=True,
        env=env,
    )
    assert commit_oid(intent) == result.stdout.decode().strip()


async def test_intent_reuse_stale_receipt_and_mutated_persisted_payload(database, tmp_path):
    _, store, intent, _ = await publication_case(database, tmp_path / "workspaces")
    assert await store.ensure(intent.task_id, intent.decision_id, intent.candidate_id) == intent
    _, receipt, _ = await store.load(intent.publish_intent_id)
    saved = await store.save(receipt.model_copy(update={"phase": "unknown"}))
    with pytest.raises(PublicationConflict, match="stale_publication_receipt"):
        await store.save(receipt)
    assert (await store.load(intent.publish_intent_id))[1] == saved
    async with database.sessions() as session, session.begin():
        row = await session.get(PublishIntentRecord, intent.publish_intent_id)
        row.payload = {**row.payload, "branch": "unapproved-branch"}
    with pytest.raises(PublicationConflict, match="corrupt_publish_intent"):
        await store.load(intent.publish_intent_id)


async def test_ch05a_upgrade_preserves_data_and_does_not_fabricate_intents(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'old.db'}"
    await ensure_database_schema(url, revision="20261021_0005")
    database = Database(url)
    try:
        task = await database.create_task(
            TaskCreate(
                repository_url="demo://buggy-calculator",
                issue_title="Historical task",
                issue_body="Keep historical state without authorizing a publication.",
            )
        )
        await ensure_database_schema(url)
        await ensure_database_schema(url)
        assert await current_database_revisions(url) == (HEAD_REVISION,)
        assert (await database.get_task(task.id)) == task
        async with database.sessions() as session:
            assert (await session.execute(select(PublishIntentRecord))).scalars().all() == []
    finally:
        await database.close()


async def test_receipt_api_is_protected_and_available_after_cancellation(
    database,
    tmp_path,
    monkeypatch,
):
    settings, store, intent, _ = await publication_case(database, tmp_path / "workspaces")
    settings.api_key = "fixture-api-key"
    for key, value in {
        "settings": settings,
        "database": database,
        "events": None,
        "queue": None,
        "redis": None,
    }.items():
        monkeypatch.setattr(app.state, key, value, raising=False)
    url = f"/api/v1/tasks/{intent.task_id}/publication"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        assert (await client.get(url)).status_code == 401
        headers = {"X-API-Key": settings.api_key}
        receipt = (await store.load(intent.publish_intent_id))[1].model_dump(mode="json")
        assert (await client.get(url, headers=headers)).json() == [receipt]
        assert (
            await client.get(url.replace(intent.task_id, "other"), headers=headers)
        ).status_code == 404


class DemoWorkspace(WorkspaceManager):
    async def prepare(self, task_id, repository_url, base_branch):
        return await super().prepare(task_id, "demo://buggy-calculator", base_branch)


@pytest.mark.parametrize("scenario", ["publish", "base_moved", "legacy_finalizing"])
async def test_graph_uses_persistent_decision_and_reports_publication_outcome(
    database,
    tmp_path,
    scenario,
):
    settings = Settings(
        _env_file=None,
        model_provider="mock",
        workspace_root=tmp_path / "workspaces",
        demo_repository_root=Path(__file__).parents[1] / "examples" / "buggy_calculator",
        sandbox_backend="local",
        github_write_enabled=True,
        github_token="fixture-credential",  # noqa: S106 -- test transport only.
        github_allowed_owners="example",
    )
    task = await database.create_task(
        TaskCreate(
            repository_url="https://github.com/example/project",
            issue_title="Fix add returning wrong result",
            issue_body="The add(a, b) function subtracts b instead of adding it.",
        )
    )
    running = await database.transition_task(
        task.id,
        expected_status=task.status,
        expected_version=task.state_version,
        status=TaskStatus.RUNNING,
    )
    approvals = ApprovalStore(database)
    server = GitHubServer("0" * 40)
    publisher = GitHubPublisher(settings, httpx.MockTransport(server))
    deps = GraphDependencies(
        settings=settings,
        model=build_agent_model(settings),
        workspaces=DemoWorkspace(settings),
        sandbox=LocalSandbox(settings),
        publisher=publisher,
        approvals=approvals,
    )
    graph = build_graph(deps, InMemorySaver())
    config = {"configurable": {"thread_id": task.graph_thread_id}}
    from repopilot.worker import Worker

    waiting = await graph.ainvoke(Worker._initial_state(task), config=config)
    assert waiting["__interrupt__"] and not server.requests
    from repopilot.candidates import Candidate

    await approvals.open_request(
        task.id,
        expected_version=running.state_version,
        candidate=Candidate.model_validate(waiting["candidate"]),
        evidence=Evidence.model_validate(waiting["test_evidence"]),
        summary=result_summary(waiting),
    )
    approval = await approvals.current(task.id)
    await approvals.decide(task.id, decision_request(approval), actor="operator")
    queued = await database.get_task(task.id)
    resumed = await database.transition_task(
        task.id,
        expected_status=queued.status,
        expected_version=queued.state_version,
        status=TaskStatus.RUNNING,
    )
    resume = await approvals.resume_for_running_task(resumed)
    # The original checkout is no longer the publication source.
    (Path(waiting["workspace"]) / "calculator.py").write_text("UNAPPROVED = True\n")
    move_base = scenario == "base_moved"
    server.state["base"] = "f" * 40 if move_base else approval.candidate.base_sha
    if scenario == "legacy_finalizing":
        await graph.ainvoke(Command(resume=resume), config=config, interrupt_after=["approval"])
        await graph.aupdate_state(
            config, {"publication_protocol_version": "legacy"}, as_node="approval"
        )
        with pytest.raises(ApprovalConflict, match="legacy_publication_requires_reconciliation"):
            await graph.ainvoke(None, config=config)
        assert not server.requests
        return
    result = await graph.ainvoke(Command(resume=resume), config=config)
    assert result["status"] == ("failed" if move_base else "completed")
    assert result["publish_receipt"]["phase"] == (
        "revalidation_required" if move_base else "published"
    )
    assert result_summary(result)["publish_receipt"] == result["publish_receipt"]
    assert len(server.state["pulls"]) == (0 if move_base else 1)
