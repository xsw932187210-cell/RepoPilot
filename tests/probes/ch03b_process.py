"""Hard process exit after a test transport applies one remote effect."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from publication_helpers import GitHubServer  # noqa: E402

from repopilot.candidates import ArtifactStore  # noqa: E402
from repopilot.config import Settings  # noqa: E402
from repopilot.db import Database  # noqa: E402
from repopilot.github import GitHubPublisher  # noqa: E402
from repopilot.publication import PublicationStore  # noqa: E402


async def run():
    stage, intent_id, root, state_file = sys.argv[1:]
    database = Database(os.environ["REPOPILOT_CH03B_POSTGRES_TEST_URL"])
    settings = Settings(
        _env_file=None,
        workspace_root=Path(root),
        github_write_enabled=True,
        github_token="fixture-credential",  # noqa: S106 -- local transport only.
        github_allowed_owners="example",
    )
    store = PublicationStore(database, ArtifactStore(Path(root) / ".artifacts"))
    intent, _, _ = await store.load(intent_id)
    server = GitHubServer(intent.approved_base_sha, Path(state_file))

    async def exit_after_write(observed):
        if observed == stage:
            os._exit(92)

    server.after_write = exit_after_write
    try:
        await GitHubPublisher(settings, httpx.MockTransport(server), store=store).publish(
            intent_id=intent_id,
        )
        raise AssertionError("Expected the probe to exit at the requested stage")
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(run())
