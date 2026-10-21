from __future__ import annotations

import base64

import httpx
import pytest
from publication_helpers import GitHubServer, publication_case

from repopilot.approvals import ApprovalRecord, DecisionRecord
from repopilot.candidates import ArtifactStore
from repopilot.config import Settings
from repopilot.db import Database
from repopilot.generations import GenerationConflict, GenerationCoordinator, SQLGenerationStore
from repopilot.github import GitHubPublisher
from repopilot.models import FileEdit, TaskStatus
from repopilot.publication import PublicationConflict, PublicationStore
from repopilot.repository import WorkspaceManager
from repopilot.security import SecurityError


@pytest.fixture
async def case(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'publish.db'}")
    await database.setup()
    try:
        settings, store, intent, approval = await publication_case(
            database, tmp_path / "workspaces"
        )
        server = GitHubServer(intent.approved_base_sha)
        publisher = GitHubPublisher(settings, httpx.MockTransport(server), store=store)
        yield database, settings, store, intent, approval, server, publisher
    finally:
        await database.close()


async def test_full_approved_tree_and_fixed_parent_are_published_once(case):
    _, _, store, intent, approval, server, publisher = case
    first = await publisher.publish(intent_id=intent.publish_intent_id)
    posts = server.posts.copy()
    second = await publisher.publish(intent_id=intent.publish_intent_id)
    assert first.published and second.published
    assert first.pull_request_url == second.pull_request_url
    assert first.receipt.phase == second.receipt.phase == "published"
    assert intent.task_id in first.branch and intent.publish_intent_id in first.branch
    assert server.posts == posts and server.posts["pr"] == 1
    assert server.state["commits"][intent.commit_sha]["parents"] == [
        {"sha": intent.approved_base_sha}
    ]
    entries = server.state["trees"][intent.tree_sha]["tree"]
    actual = {entry["path"] for entry in entries if entry["type"] == "blob"}
    assert actual == {
        item["path"] for item in approval.candidate.manifest["entries"] if item["kind"] == "file"
    }
    with store.artifacts.materialize(approval.candidate) as root:
        for entry in entries:
            if entry["type"] == "blob":
                assert (
                    base64.b64decode(server.state["blobs"][entry["sha"]]["content"])
                    == (root / entry["path"]).read_bytes()
                )
    assert all(
        req.headers["Authorization"] == "Bearer fixture-credential" for req in server.requests
    )


@pytest.mark.parametrize("stage", ["blob", "tree", "commit", "ref", "pr"])
async def test_response_loss_reopens_database_and_converges_to_one_pr(case, stage):
    database, settings, _, intent, _, server, publisher = case
    server.lose = stage
    first = await publisher.publish(intent_id=intent.publish_intent_id)
    assert first.receipt.phase == "unknown"
    assert "sending" in first.receipt.steps.values() or "unknown" in first.receipt.steps.values()
    await database.close()
    reopened = Database(database.url)
    try:
        store = PublicationStore(reopened, ArtifactStore(settings.workspace_root / ".artifacts"))
        server.unavailable = False
        restored = GitHubPublisher(settings, httpx.MockTransport(server), store=store)
        result = await restored.publish(intent_id=intent.publish_intent_id)
        assert result.published and result.receipt.phase == "published"
        assert len(server.state["pulls"]) == server.posts["pr"] == 1
        if stage != "blob":
            assert server.posts[stage] == 1
        assert (await store.load(intent.publish_intent_id))[1] == result.receipt
    finally:
        await reopened.close()


async def test_ambiguous_pr_with_empty_lookup_is_never_recreated(case):
    _, _, _, intent, _, server, publisher = case
    server.lose = "pr"
    result = await publisher.publish(intent_id=intent.publish_intent_id)
    assert result.receipt.phase == "unknown"
    server.unavailable, server.hide_pr = False, True
    for _ in range(2):
        result = await publisher.publish(intent_id=intent.publish_intent_id)
        assert result.receipt.phase == "unknown" and server.posts["pr"] == 1
    server.hide_pr = False
    result = await publisher.publish(intent_id=intent.publish_intent_id)
    assert result.published and server.posts["pr"] == 1


@pytest.mark.parametrize("when", ["before", "during-ref", "during-pr", "lost-pr"])
async def test_cancel_reconciles_inflight_effects_without_sending_next_request(case, when):
    _, _, _, intent, _, server, publisher = case
    cancel = when == "before"

    async def cancelled():
        return cancel

    async def after_write(stage):
        nonlocal cancel
        if stage == ("ref" if when == "during-ref" else "pr"):
            cancel = True

    if when != "before":
        server.after_write = after_write
    if when == "lost-pr":
        server.lose = "pr"
    result = await publisher.publish(intent_id=intent.publish_intent_id, cancelled=cancelled)
    if when == "lost-pr":
        assert result.receipt.phase == "unknown"
        server.unavailable = False
        result = await publisher.publish(intent_id=intent.publish_intent_id, cancelled=cancelled)
    assert result.receipt.phase == "cancelled" and result.receipt.cancel_requested
    assert result.published == (when in {"during-pr", "lost-pr"})
    if when == "before":
        assert not server.posts
    if when == "during-ref":
        assert result.receipt.remote_ids["ref"] and server.posts["pr"] == 0
    posts = server.posts.copy()
    await publisher.publish(intent_id=intent.publish_intent_id, cancelled=cancelled)
    assert server.posts == posts


@pytest.mark.parametrize("when", ["before", "during-ref", "during-pr", "lost-pr"])
async def test_base_movement_requires_revalidation_and_keeps_observed_effects(case, when):
    _, _, _, intent, _, server, publisher = case
    moved = "f" * 40
    if when == "before":
        server.state["base"] = moved

    async def after_write(stage):
        if stage == ("ref" if when == "during-ref" else "pr"):
            server.state["base"] = moved

    if when != "before":
        server.after_write = after_write
    if when == "lost-pr":
        server.lose = "pr"
    result = await publisher.publish(intent_id=intent.publish_intent_id)
    if when == "lost-pr":
        server.unavailable = False
        result = await publisher.publish(intent_id=intent.publish_intent_id)
    assert result.receipt.phase == "revalidation_required"
    assert result.receipt.requires_revalidation and moved in result.receipt.observed_base_shas
    assert result.published == (when in {"during-pr", "lost-pr"})
    posts = server.posts.copy()
    server.state["base"] = intent.approved_base_sha
    again = await publisher.publish(intent_id=intent.publish_intent_id)
    assert again.receipt.requires_revalidation and server.posts == posts


async def test_conflicting_branch_is_never_overwritten(case):
    _, _, _, intent, _, server, publisher = case
    server.state["refs"][intent.branch] = {
        "ref": f"refs/heads/{intent.branch}",
        "object": {"type": "commit", "sha": "f" * 40},
    }
    result = await publisher.publish(intent_id=intent.publish_intent_id)
    assert result.receipt.phase == "blocked" and result.reason == "remote_branch_conflict"
    assert server.posts["ref"] == server.posts["pr"] == 0


@pytest.mark.parametrize("fault", ["head", "base", "draft", "closed", "body", "duplicate"])
async def test_conflicting_pr_is_not_accepted_or_replaced(case, fault):
    _, _, _, intent, _, server, publisher = case
    await publisher.publish(intent_id=intent.publish_intent_id)
    pr = server.state["pulls"][0]
    if fault == "head":
        pr["head"]["sha"] = "f" * 40
    elif fault == "base":
        pr["base"]["ref"] = "other"
    elif fault == "draft":
        pr["draft"] = False
    elif fault == "closed":
        pr["state"] = "closed"
    elif fault == "body":
        pr["body"] = "unrelated"
    else:
        server.state["pulls"].append(pr.copy())
    posts = server.posts.copy()
    result = await publisher.publish(intent_id=intent.publish_intent_id)
    assert result.receipt.phase == "blocked" and result.reason == "remote_pr_conflict"
    assert result.published and server.posts == posts


async def test_lookup_unavailability_or_truncation_never_authorizes_writes(case):
    _, _, _, intent, _, server, publisher = case
    server.unavailable = True
    assert (await publisher.publish(intent_id=intent.publish_intent_id)).receipt.phase == "unknown"
    assert not server.posts
    server.unavailable = False
    await publisher.publish(intent_id=intent.publish_intent_id)
    posts = server.posts.copy()
    server.truncate_tree = True
    result = await publisher.publish(intent_id=intent.publish_intent_id)
    assert result.receipt.phase == "unknown" and result.reason == "remote_tree_incomplete"
    assert server.posts == posts


@pytest.mark.parametrize(
    "fault",
    ["disabled", "token", "owner", "approval", "artifact", "legacy_decision", "stopped"],
)
async def test_invalid_authority_or_artifact_never_sends_http(case, fault):
    database, settings, _, intent, approval, server, publisher = case
    if fault == "disabled":
        settings.github_write_enabled = False
    elif fault == "token":
        settings.github_token = ""
    elif fault == "owner":
        settings.github_allowed_owners = "other"
    elif fault == "approval":
        async with database.sessions() as session, session.begin():
            row = await session.get(ApprovalRecord, approval.approval_id)
            row.status = "cancelled"
    elif fault == "legacy_decision":
        async with database.sessions() as session, session.begin():
            row = await session.get(DecisionRecord, intent.decision_id)
            row.payload = {**row.payload, "publication_protocol_version": None}
    elif fault == "stopped":
        task = await database.get_task(intent.task_id)
        await database.transition_task(
            task.id,
            expected_status=task.status,
            expected_version=task.state_version,
            status=TaskStatus.CANCELLED,
        )
    else:
        bundle = settings.workspace_root / ".artifacts" / approval.candidate.artifact_digest
        (bundle / "tree.tar").write_bytes(b"tampered")
    if fault in {"disabled", "token"}:
        result = await publisher.publish(intent_id=intent.publish_intent_id)
        assert not result.published
    else:
        with pytest.raises((PublicationConflict, SecurityError)):
            await publisher.publish(intent_id=intent.publish_intent_id)
    assert not server.requests


async def test_readonly_reconciliation_after_task_cancellation_keeps_late_pr(case):
    database, _, _, intent, _, server, publisher = case
    server.lose = "pr"
    await publisher.publish(intent_id=intent.publish_intent_id)
    task = await database.get_task(intent.task_id)

    await database.transition_task(
        task.id,
        expected_status=task.status,
        expected_version=task.state_version,
        status=TaskStatus.CANCELLED,
    )
    server.unavailable = False
    posts = server.posts.copy()
    result = await publisher.publish(intent_id=intent.publish_intent_id, reconcile_only=True)
    assert result.published and result.receipt.remote_ids["pr_number"] == 1
    assert server.posts == posts
    assert (await database.get_task(task.id)).status == TaskStatus.CANCELLED


async def test_raw_edits_and_unpersisted_intents_are_refused():
    publisher = GitHubPublisher(Settings(_env_file=None))
    with pytest.raises(PublicationConflict, match="persistent_publish_intent_required"):
        await publisher.publish(intent_id="0" * 64)
    with pytest.raises(TypeError):
        await publisher.publish(task_id="task", edits=[])


async def test_superseded_candidate_cannot_start_publication(case):
    database, settings, store, intent, approval, server, publisher = case
    with store.artifacts.materialize(approval.candidate) as snapshot:
        contents = (snapshot / "calculator.py").read_text()
    coordinator = GenerationCoordinator(
        store.artifacts,
        SQLGenerationStore(database),
        WorkspaceManager(settings),
    )
    await coordinator.apply(
        approval.candidate,
        [FileEdit(path="calculator.py", content=contents + "\n# next candidate\n")],
        expected_contents={"calculator.py": contents},
        base_repository=settings.workspace_root / intent.task_id,
    )
    with pytest.raises(GenerationConflict, match="stale_candidate_generation"):
        await publisher.publish(intent_id=intent.publish_intent_id)
    assert not server.requests


@pytest.mark.parametrize("stage", ["blob", "tree", "commit", "ref", "pr"])
async def test_successful_effect_with_lost_response_is_reconciled_in_same_run(case, stage):
    _, settings, store, intent, _, server, _ = case
    lost = False

    async def transport(request):
        nonlocal lost
        response = await server(request)
        paths = {
            "blob": "git/blobs",
            "tree": "git/trees",
            "commit": "git/commits",
            "ref": "git/refs",
            "pr": "pulls",
        }
        if not lost and request.method == "POST" and request.url.path.endswith(paths[stage]):
            lost = True
            raise httpx.ReadTimeout("Applied but response lost", request=request)
        return response

    result = await GitHubPublisher(settings, httpx.MockTransport(transport), store=store).publish(
        intent_id=intent.publish_intent_id,
    )
    assert lost and result.published and result.receipt.phase == "published"
    assert server.posts["blob"] == len(server.state["blobs"])
    assert all(server.posts[name] == 1 for name in ("tree", "commit", "ref", "pr"))


@pytest.mark.parametrize("stage", ["ref", "pr"])
async def test_simultaneous_cancel_and_base_movement_preserves_both_facts(case, stage):
    _, _, _, intent, _, server, publisher = case
    cancel = False

    async def cancelled():
        return cancel

    async def after_write(observed):
        nonlocal cancel
        if observed == stage:
            cancel = True
            server.state["base"] = "f" * 40

    server.after_write = after_write
    result = await publisher.publish(intent_id=intent.publish_intent_id, cancelled=cancelled)
    assert result.receipt.cancel_requested and result.receipt.requires_revalidation
    assert result.receipt.phase == "cancelled"
    assert "f" * 40 in result.receipt.observed_base_shas
    assert result.published == (stage == "pr")
    assert server.posts["pr"] == (1 if stage == "pr" else 0)


async def test_cancel_remains_sticky_when_lost_pr_response_is_followed_by_empty_lookup(case):
    _, settings, store, intent, _, server, _ = case
    cancel = False

    async def cancelled():
        return cancel

    async def transport(request):
        nonlocal cancel
        response = await server(request)
        if request.method == "POST" and request.url.path.endswith("/pulls"):
            cancel, server.hide_pr = True, True
            raise httpx.ReadTimeout(
                "Applied; response lost and lookup not yet visible", request=request
            )
        return response

    publisher = GitHubPublisher(settings, httpx.MockTransport(transport), store=store)
    result = await publisher.publish(intent_id=intent.publish_intent_id, cancelled=cancelled)
    assert result.receipt.phase == "unknown" and result.receipt.cancel_requested
    cancel, server.hide_pr = False, False
    result = await publisher.publish(intent_id=intent.publish_intent_id, cancelled=cancelled)
    assert result.published and result.receipt.phase == "cancelled"
    assert server.posts["pr"] == 1
