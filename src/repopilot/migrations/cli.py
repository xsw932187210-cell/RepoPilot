from __future__ import annotations

import argparse
import asyncio

from repopilot.config import Settings
from repopilot.migrations import current_database_revisions, ensure_database_schema


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the RepoPilot application schema")
    parser.add_argument("command", choices=("upgrade", "current"))
    parser.add_argument("--database-url", help="Override DATABASE_URL")
    parser.add_argument("--revision", default="head", help="Alembic target revision")
    return parser


async def run(args: argparse.Namespace) -> None:
    database_url = args.database_url or Settings().database_url
    if args.command == "upgrade":
        before = await ensure_database_schema(database_url, revision=args.revision)
        revisions = await current_database_revisions(database_url)
        print(f"schema_before={before.kind.value} revision={','.join(revisions)}")
        return
    revisions = await current_database_revisions(database_url)
    print(",".join(revisions) if revisions else "unversioned")


def main() -> None:
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
