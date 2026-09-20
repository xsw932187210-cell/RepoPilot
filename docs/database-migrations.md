# Database migrations and task state versions

RepoPilot owns a small application schema and upgrades it with Alembic. The migration boundary is
deliberately narrow: RepoPilot revisions may change `tasks`, `task_events`, and
`alembic_version`; they do not inspect or rewrite LangGraph checkpoint tables.

## Ownership boundary

| Data | Owner and upgrade entry point |
| --- | --- |
| Task metadata, status, result, and event history | RepoPilot Alembic revisions under `src/repopilot/migrations/`; run `repopilot-migrate upgrade` |
| LangGraph checkpoints | `langgraph-checkpoint-postgres`; the worker calls the provider-supported `AsyncPostgresSaver.setup()` entry point |
| Redis queue, cancellation keys, locks, and Pub/Sub | Ephemeral coordination state; not part of a database migration |

Do not add guessed LangGraph columns to a RepoPilot revision. When the checkpoint dependency needs
an upgrade, follow that dependency's supported migration/setup path and test checkpoint compatibility
separately.

## Revision history

| Revision | Meaning |
| --- | --- |
| `20260921_0001` | Exact baseline for the application tables that existed before CH-09 |
| `20260921_0002` | Adds optimistic task-state versioning and owned-JSON schema versions |

`Database.setup()` now runs the same upgrade path as the CLI. Compose also has a one-shot `migrate`
service, and API/Worker startup waits for that service to finish successfully.

The installer handles these inputs:

- An empty database runs the baseline and all later revisions.
- An unversioned database is stamped at `20260921_0001` only when both RepoPilot tables have the
  exact legacy column sets. It is then upgraded normally.
- A versioned database is upgraded from its recorded revision.
- A partial or modified unversioned RepoPilot schema is refused. Startup will not guess which
  changes are safe.
- Re-running `upgrade` at the current head is idempotent and does not rewrite application rows.

No planned tables or fields for later change cards are created in these revisions.

## Defaults and old-record compatibility

Revision `20260921_0002` adds three non-null integer columns:

| Column | Migrated legacy value | New-write value | Meaning |
| --- | --- | --- | --- |
| `tasks.state_version` | `1` | `1`, then incremented by each accepted state transition | Optimistic state version |
| `tasks.result_schema_version` | `0` | `1` when a current result is created or replaced | Schema of RepoPilot-owned result JSON |
| `task_events.payload_schema_version` | `0` | `1` | Schema of RepoPilot-owned event payload JSON |

The migration does not reinterpret old JSON. Version `0` means the pre-versioning shape and remains
readable as a dictionary; current metrics and event readers tolerate missing optional keys. Version
`1` is the current writer contract. Future incompatible JSON changes must add a new version and an
explicit decoder or forward migration rather than silently treating old bytes as the new shape.

The migration does not add approval or owner fields. Unknown future values therefore cannot become
an approval or ownership grant through a permissive default.

## Task state contract

Every task state update supplies the status and version that the caller observed. The database
executes one conditional update and increments `state_version` only when both still match.

| Current status | Allowed next status |
| --- | --- |
| `queued` | `running`, `cancelled` |
| `running` | `awaiting_approval`, `completed`, `failed`, `cancelled` |
| `awaiting_approval` | `queued`, `cancelled` |
| `completed`, `failed`, `cancelled` | none |

An illegal edge raises `InvalidTaskTransition`. A stale status or version raises
`TaskStateConflict` with the expected and current state. API races are returned as HTTP `409`; a
Worker that loses a race discards its stale database update instead of overwriting the winner's
terminal state.

This is application-state compare-and-set, not a Worker fencing token. It does not prevent an old
Worker from changing a workspace or performing an external side effect before its final database
write is rejected. Redis delivery, task rows, events, checkpoints, workspaces, and remote APIs also
do not share one transaction. Those remaining guarantees belong to the later queue, ownership,
candidate, and publication change cards.

## Deployment upgrade order

Old and new API/Worker processes must not be mixed. Old processes do not increment
`state_version`, so their unconditional writes would bypass the new concurrency contract.

For an existing deployment:

1. Stop all old API and Worker processes and stop accepting new jobs.
2. Confirm no task is actively executing and take a database backup appropriate to the deployment.
3. Deploy the new image and run exactly one migrator: `repopilot-migrate upgrade`. With Compose,
   `docker compose run --rm migrate` uses the same entry point.
4. Verify `repopilot-migrate current` reports `20260921_0002`.
5. Start only the new API and Worker image, then run the normal smoke checks.

A fresh `docker compose up --build -d` performs step 3 through the one-shot `migrate` service. Do
not run multiple first-time migrators concurrently against the same unversioned database.

## Failure recovery

Destructive downgrade is intentionally unsupported. Recovery is backup restore or a reviewed
forward fix.

- PostgreSQL applies the CH-09 DDL inside a migration transaction. If a revision fails, correct the
  cause and rerun `repopilot-migrate upgrade`; the version remains at the last completed revision.
  An exact legacy database may already be stamped at `20260921_0001`, which is a valid retry point.
- If inspection reports a partial or unknown unversioned schema, do not stamp it manually. Restore
  the pre-upgrade backup or write and review a migration for that exact structure.
- SQLite should also be backed up before an upgrade. If an interrupted SQLite DDL operation leaves
  a structure that no longer matches a known revision, restore the backup and rerun; do not use a
  destructive downgrade as repair.
- If a deployed revision itself is wrong, stop writers and ship a new forward revision. Never edit
  a revision that may already have run in another environment.

The PostgreSQL integration test proves the documented retry path by starting from real legacy
tables and rows, rolling back a deliberately failed DDL transaction, rerunning to head, and checking
that the task and event remain readable.

## Verification commands

SQLite migration, compatibility, conflict, and interruption tests are part of the regular suite:

```bash
pytest -q tests/test_migrations.py
```

The PostgreSQL check refuses any database not named `repopilot_ch09_test`. Its wrapper creates a
unique Compose project, dedicated host port and volume, then removes only those resources:

```bash
make migration-test
```

Override the defaults only when the replacement values are also dedicated to this test:

```bash
CH09_POSTGRES_PORT=55440 CH09_COMPOSE_PROJECT=repopilot_ch09_manual make migration-test
```
