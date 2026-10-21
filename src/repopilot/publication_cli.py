"""Explicit same-intent recovery. Default is read-only remote reconciliation."""

from __future__ import annotations

import argparse
import asyncio

from repopilot.candidates import ArtifactStore
from repopilot.config import get_settings
from repopilot.db import Database
from repopilot.github import GitHubPublisher
from repopilot.publication import PublicationStore


async def reconcile(intent_id: str, *, continue_active: bool) -> int:
    settings = get_settings()
    database = Database(settings.database_url)
    try:
        store = PublicationStore(database, ArtifactStore(settings.workspace_root / ".artifacts"))
        result = await GitHubPublisher(settings, store=store).publish(
            intent_id=intent_id,
            reconcile_only=not continue_active,
        )
        print(result.receipt.model_dump_json())
        return 0 if result.receipt.phase == "published" else 1
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("intent_id")
    parser.add_argument(
        "--continue-active",
        action="store_true",
        help="Continue the same intent only while its original SQL task/approval remain active",
    )
    arguments = parser.parse_args()
    raise SystemExit(
        asyncio.run(
            reconcile(
                arguments.intent_id,
                continue_active=arguments.continue_active,
            )
        )
    )


if __name__ == "__main__":
    main()
