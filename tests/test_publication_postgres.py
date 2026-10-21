from __future__ import annotations

import asyncio
import os
import sys
import uuid

import asyncpg
import httpx
import pytest
from publication_helpers import GitHubServer, publication_case
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from repopilot.candidates import ArtifactStore
from repopilot.db import Database
from repopilot.github import GitHubPublisher
from repopilot.migrations import HEAD_REVISION, current_database_revisions, ensure_database_schema
from repopilot.models import TaskCreate
from repopilot.publication import PublicationConflict, PublicationStore, PublishIntentRecord

POSTGRES_URL = os.getenv("REPOPILOT_CH03B_POSTGRES_TEST_URL", "")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="isolated CH-03B PostgreSQL required")


async def database():
    assert make_url(POSTGRES_URL).database == "repopilot_ch03b_test"
    db = Database(POSTGRES_URL)
    await db.setup()
    return db


@pytest.mark.parametrize("stage", ["blob", "tree", "commit", "ref", "pr"])
async def test_hard_exit_after_effect_reopens_postgres_and_reuses_exact_targets(tmp_path, stage):
    db = await database()
    root, remote = tmp_path / "workspaces", tmp_path / "remote.json"
    try:
        settings, store, intent, _ = await publication_case(db, root)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "tests/probes/ch03b_process.py",
            stage,
            intent.publish_intent_id,
            str(root),
            str(remote),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        assert process.returncode == 92, (stdout.decode(), stderr.decode())
        await db.close()
        db = await database()
        store = PublicationStore(db, ArtifactStore(root / ".artifacts"))
        _, before, _ = await store.load(intent.publish_intent_id)
        assert "sending" in before.steps.values()
        server = GitHubServer(intent.approved_base_sha, remote)
        result = await GitHubPublisher(settings, httpx.MockTransport(server), store=store).publish(
            intent_id=intent.publish_intent_id,
        )
        assert result.published and result.receipt.phase == "published"
        assert server.posts["blob"] == len(server.state["blobs"])
        assert all(server.posts[name] == 1 for name in ("tree", "commit", "ref", "pr"))
        assert len(server.state["pulls"]) == 1
        async with db.sessions() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(PublishIntentRecord)
                .where(
                    PublishIntentRecord.task_id == intent.task_id,
                )
            )
        assert count == 1
        print(f"CH-03B hard_exit=92 after={stage} intents=1 draft_pr=1 fixed_parent=true")
    finally:
        await db.close()


async def test_postgres_same_decision_initialization_and_receipt_cas_races(tmp_path):
    db = await database()
    try:
        _, store, intent, _ = await publication_case(db, tmp_path / "workspaces")
        results = await asyncio.gather(
            *[
                store.ensure(intent.task_id, intent.decision_id, intent.candidate_id)
                for _ in range(2)
            ]
        )
        assert results == [intent, intent]
        _, receipt, _ = await store.load(intent.publish_intent_id)
        results = await asyncio.gather(
            *[
                store.save(receipt.model_copy(update={"phase": phase}))
                for phase in ("publishing", "unknown")
            ],
            return_exceptions=True,
        )
        assert sum(isinstance(item, PublicationConflict) for item in results) == 1
        assert (await store.load(intent.publish_intent_id))[1].version == receipt.version + 1
        print("CH-03B initialization=idempotent receipt_cas_winners=1 stale_writers=1")
    finally:
        await db.close()


async def test_postgres_ch05a_upgrade_preserves_records_and_installs_empty_intent_table():
    assert make_url(POSTGRES_URL).database == "repopilot_ch03b_test"
    name = f"repopilot_ch03b_migration_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(POSTGRES_URL.replace("+asyncpg", ""))
    url = make_url(POSTGRES_URL).set(database=name).render_as_string(hide_password=False)
    db = Database(url)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        await ensure_database_schema(url, revision="20261021_0005")
        task = await db.create_task(
            TaskCreate(
                repository_url="demo://buggy-calculator",
                issue_title="CH-05A upgrade fixture",
                issue_body="Preserve metadata without inferring an old publication outcome.",
            )
        )
        await db.add_event(task.id, kind="upgrade", node="probe", message="retained")
        await db.setup()
        await db.setup()
        assert await current_database_revisions(url) == (HEAD_REVISION,)
        assert (await db.get_task(task.id)) == task
        assert len(await db.list_events(task.id)) == 1
        async with db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(PublishIntentRecord)) == 0
            version = (await session.execute(text("SHOW server_version"))).scalar_one()
        print(f"CH-03B migration=0005->{HEAD_REVISION} postgres={version} retained=true intents=0")
    finally:
        await db.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
