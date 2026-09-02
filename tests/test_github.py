from __future__ import annotations

import json

import httpx
import pytest

from repopilot.config import Settings
from repopilot.github import GitHubPublisher
from repopilot.models import FileEdit
from repopilot.security import SecurityError


@pytest.mark.asyncio
async def test_github_publisher_builds_a_draft_pr_after_governance() -> None:
    requests: list[httpx.Request] = []
    blob_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal blob_count
        requests.append(request)
        path = request.url.path
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "base-sha"}})
        if path.endswith("/git/commits/base-sha"):
            return httpx.Response(200, json={"tree": {"sha": "base-tree"}})
        if path.endswith("/git/blobs"):
            blob_count += 1
            return httpx.Response(201, json={"sha": f"blob-{blob_count}"})
        if path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "new-tree"})
        if path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "new-commit"})
        if path.endswith("/git/refs"):
            return httpx.Response(201, json={"ref": "refs/heads/repopilot/task-123"})
        if path.endswith("/pulls"):
            payload = json.loads(request.content)
            assert payload["draft"] is True
            assert payload["base"] == "main"
            return httpx.Response(
                201,
                json={"html_url": "https://github.com/example/project/pull/7"},
            )
        raise AssertionError(f"Unexpected GitHub request: {request.method} {path}")

    settings = Settings(
        github_write_enabled=True,
        github_token="fixture-credential",  # noqa: S106 - non-secret test value
        github_allowed_owners="example",
    )
    publisher = GitHubPublisher(settings, httpx.MockTransport(handler))
    result = await publisher.publish(
        task_id="task-12345678",
        repository_url="https://github.com/example/project",
        base_branch="main",
        issue_title="Fix parser boundary",
        edits=[FileEdit(path="parser.py", content="VALUE = 1\n")],
    )

    assert result.published is True
    assert result.pull_request_url == "https://github.com/example/project/pull/7"
    assert result.branch == "repopilot/task-123"
    assert len(requests) == 7
    assert all(
        request.headers["Authorization"] == "Bearer fixture-credential"
        for request in requests
    )


@pytest.mark.asyncio
async def test_github_publisher_is_disabled_by_default() -> None:
    publisher = GitHubPublisher(Settings())
    result = await publisher.publish(
        task_id="task-1",
        repository_url="https://github.com/example/project",
        base_branch="main",
        issue_title="No external write",
        edits=[],
    )
    assert result.published is False
    assert result.reason == "GitHub writes are disabled"


@pytest.mark.asyncio
async def test_github_publisher_rejects_non_allowlisted_owner() -> None:
    settings = Settings(
        github_write_enabled=True,
        github_token="fixture-credential",  # noqa: S106 - non-secret test value
        github_allowed_owners="approved-owner",
    )
    publisher = GitHubPublisher(settings)
    with pytest.raises(SecurityError):
        await publisher.publish(
            task_id="task-1",
            repository_url="https://github.com/other-owner/project",
            base_branch="main",
            issue_title="Reject this target",
            edits=[],
        )
