# Confirmed cancellation and task deadlines (CH-06A)

The SQL task record owns stop requests. Redis cancellation keys are hints. Production Worker
execution binds one `ExecutionControl` to the graph, model caller, subprocess runner and sandbox.
Task status/version CAS still applies; this protocol does not add a Worker ownership epoch.

## API and confirmation

`POST /api/v1/tasks/{id}/cancel` returns the `TaskView`:

| Prior state | Response | Meaning |
| --- | --- | --- |
| `queued` or `awaiting_approval`, no owned resources | 200, `cancelled`, `stopped_at` set | SQL CAS prevented an executor from starting; approval requests/delivery intents are blocked |
| `running` | 202, `cancelling`, `stop_requested_at` set, `stopped_at=null` | Durable request accepted; execution cleanup and publication reconciliation are still pending |
| `cancelling` | 202, same request/version | Idempotent request; this response does not confirm a stop |
| Terminal | 200, original record | Terminal outcomes are immutable; an old migrated `cancelled` record may have no stop confirmation |

After active waits finish and owned resources are removed, the same executor confirms
`cancelling → cancelled` using CAS. Poll GET task or read event history for `stopped_at` and
`execution_stopped`. Stop-request and confirmation events commit with their SQL state changes;
notification delivery is still subject to the existing SSE/PubSub gap (CH-06B).

Cleanup failures keep `cancelling`, `stopped_at=null`, `execution_resources` and `cleanup_pending`.
Diagnostic entries contain resource kind, exact ID, cleanup action and exception class, rather
than raw exception text or credentials. A normal terminal transition also rejects live resources.
`cancellation_requested`, `deadline_exceeded`, `cleanup_pending`, `execution_stopped` and the
attempt-scoped `attempt_aborted` event distinguish the outcomes.

## Deadline accounting

New API tasks accept `execution_timeout_seconds` (1–86400), `deadline_includes_queue` and
`deadline_includes_approval`. Omitted/null values use runtime settings:

| Setting | Default |
| --- | --- |
| `TASK_TIMEOUT_SECONDS` | 900 |
| `DEADLINE_INCLUDES_QUEUE` | false |
| `DEADLINE_INCLUDES_APPROVAL` | false |
| `CANCELLATION_POLL_SECONDS` | 0.1 |
| `PUBLICATION_RECONCILE_SECONDS` | 10 |

The policy is saved on creation. Changing environment settings does not reset an existing task's
remaining budget. `execution_elapsed_seconds`, `queue_elapsed_seconds` and
`approval_elapsed_seconds` retain closed phase intervals; `phase_started_at` identifies the
current interval. GET `remaining_seconds` includes the current counted interval. `deadline_at`
is an absolute timestamp while the current phase counts, and null while its budget is paused.
Active execution uses a monotonic deadline derived from the persisted remaining budget; persisted
timestamps assume a synchronized service clock across restarts.

Running graph nodes and model/process waits use the smaller of the operation limit and remaining
task budget. Model retries/backoff share that same scope. No new model reservation/transport starts
after expiry. A separate Worker loop checks waiting tasks every second, including while another
task is active. On expiry, idle tasks finish as `failed`; active tasks first enter `cancelling`
with `stop_reason=deadline_exceeded`, then finish as `failed` after cleanup.

The deadline bounds execution/scheduling, not the time needed to prove cleanup. Filesystem threads
are joined, subprocess groups killed and waited, and Docker threads joined before confirmation.
Docker control-plane requests and process waits have finite cleanup timeouts. If cleanup cannot be
proved, the record stays pending; it does not become successful merely because a timer expired.

## Resource and publication boundaries

Subprocesses start new sessions; only the captured invocation's process group is killed.
Candidate freezing routes its thread-based binary Git I/O through the same controlled runner.
Docker containers use a fresh execution label and task label. The exact ID returned by `create`
is persisted before `start`. Cancellation kills only that object, joins its SDK thread and checks
removal. Containers continue using snapshots, no host mounts, disabled networking and the existing
resource limits. An unrelated task's process group/container is never selected by a broad scan.

Cancelling a model wait closes the local async request. Its durable `STARTED` attempt becomes
`UNKNOWN`; reservation and usage uncertainty remain consumed across restart. This cannot prove
that the provider stopped computation or billing.

Publication reuses CH-03B's intent, predicate and receipt. Before a write it checks the stop
signal; a possibly sent request is reconciled before reporting the outcome. Only reads may use
the bounded reconciliation allowance after stop. Remote objects and an already created PR are
retained in the receipt. An unavailable read remains `unknown`; local stopped confirmation never
means that remote effects were rolled back. Read the task's `/publication` receipt separately.

`ExecutionControl.abort_attempt()` has `scope=attempt`. It interrupts this attempt without calling
SQL `request_stop` or marking the entire task cancelled. CH-04B can reuse it for ownership loss;
the ownership/fencing mechanism itself remains unimplemented.

## Upgrade and interrupted execution

Stop old API/Workers, ensure active executions have drained, back up SQL and artifacts, and run
`repopilot-migrate upgrade` through `20261022_0007` before starting the new version. SQLite and
PostgreSQL use the same Alembic chain. The migration preserves old rows and approvals, initializes
new duration counters at zero, and starts future phase accounting at upgrade time. Historical
durations, deadlines and stop confirmations are not invented: old tasks have
`execution_timeout_seconds=null` and old terminal records keep `stopped_at=null`.

On Worker restart, existing `cancelling` tasks remain unconfirmed and get retained cleanup
diagnostics. Startup does not kill persisted PIDs, adopt another execution, automatically enqueue
work or assert that the predecessor stopped. Stop all old executors first; an operator must inspect
the exact retained Docker IDs/labels or process identity, reconcile any publication intent, and
resolve cleanup before using the trusted `Database.confirm_stop` interface. Do not clear resource
evidence merely to obtain a terminal state. This is a deliberate recovery boundary until ownership
and a recovery dispatcher exist. Restore backups or apply a forward fix; destructive downgrade is
unsupported.

## Verification

`tests/test_execution_control.py` covers API 202 semantics, idle/approval cancellation, stale
completion, cleanup evidence, fake clocks for all accounting policies, model interruption and
backoff, attempt scope, joined threads, publication in flight and conservative restart reporting.
`make ch06a-integration` uses a dedicated Compose project and finite PostgreSQL/Redis/Docker probes
for migration rollback/retry, process/container stop and sibling isolation, concurrent requests,
publication reconciliation, the full suite, Mock evaluation and actual Worker approval restart.
It never calls a real model or publishes a test task to GitHub.
