# Runtime correctness and trust boundaries

This note describes the runtime guarantees that are actually implemented by the
current worker, graph, repository, event, and sandbox code. It intentionally
does not infer stronger guarantees from the names of the components. In
particular, the implementation is a single-writer workflow with read-only
parallel research/analysis branches; it is not a general concurrent filesystem
transaction system.

## Execution shape

`Worker.handle()` claims a task with a Redis lock, loads the task, and invokes a
checkpointed LangGraph thread. The graph is:

```text
prepare -> planner -> [researcher, test_analyst] -> coder -> apply_policy -> test_runner
                                                   ^            |                 |
                                                   |-- task-wide correction             reviewer
                                                                            |
                                                        approval <- remediation / failed
                                                            |
                                                     finalize or cancelled
```

The two branches after `planner` both observe the planner output. They are
read-only with respect to the workspace: `researcher` calls
`WorkspaceManager.inspect()` and `test_analyst` only constructs a test strategy.
`apply_policy` is the sole graph node that calls `apply_edits()`. This makes the graph
single-writer by design, but it does not protect the workspace from processes
outside this graph.

## Mechanism / failure mode / current action / unresolved limitation

| Area | Mechanism in the code | Failure mode or boundary | Current action | Unresolved limitation |
| --- | --- | --- | --- | --- |
| Worker ownership | `JobQueue.acquire()` uses `SET key token NX EX 900`; `release()` deletes only when the token still matches. | Duplicate delivery is skipped while the lock is live. If a run lasts beyond 900 seconds, the key can expire and another worker can acquire the same task while the first worker is still running. | Token-checked release prevents an old worker from deleting a newer owner’s lock. | There is no renewal, lease epoch, or fencing token. The lock is a best-effort lease, not proof of exclusive ownership for the whole run. |
| Queue delivery | Jobs use `LPUSH/BRPOP`. Approval decision and pending resume intent commit with task CAS before enqueue. | A worker can die after dequeue; an enqueue can succeed without its response arriving. Task creation still has no Outbox. | Approval delivery errors return a saved pending intent and explicit retry API. Worker resolves the SQL decision and claims the queued version. | `enqueued` is not acknowledgement. There is no automatic dispatcher, dequeue reclaim or Worker fencing. |
| Task status | Allowed transitions are explicit. `transition_task()` conditionally matches task ID, expected status, and `state_version`, then updates status/result and increments the version in one SQL statement. | Status writes and graph/Redis/event/workspace side effects are separate operations. A crash between them can leave a task marked `RUNNING` or leave an event/result incomplete. | Illegal edges are rejected; stale writes return a conflict. API races become `409`, and a stale Worker result cannot overwrite a newer terminal task row. | The state version is not an ownership epoch or a recovery watchdog. It does not fence workspace or remote side effects and is not a transaction across the other components. |
| Event publication | `EventBus.publish()` writes the event to the database and then publishes JSON on Redis Pub/Sub. | A database write can succeed while Redis publish fails; a subscriber can disconnect and miss Pub/Sub messages. | Persist-before-publish gives a durable event record before the live notification attempt. | Pub/Sub is not a durable delivery stream and `subscribe()` has no replay cursor in this file. Consumers must use the database separately if they need recovery. |
| Model provider calls | `ControlledModelCaller` atomically reserves one task-wide attempt, commits `STARTED`, then invokes transport with SDK retries disabled. All roles, policy corrections, and controlled retries share the persisted cap. | The reservation/started/outcome transactions and provider request are not one transaction. A process can die before send or after a request may have completed. | A pre-send reservation remains consumed. Recovery converts persisted `STARTED` attempts to `UNKNOWN`; neither state is refunded or silently retried. Stable error classes bound request timeout, retry count, one wait, and cumulative backoff. | This prevents free retries but cannot prevent provider-side duplicate effects or prove exact billing. Task CAS is not Worker fencing; a split-brain Worker remains possible until CH-04B. |
| Context and generations | Inspection materializes a bound artifact. Whole-batch preflight and descriptor-relative writes run in a new private stage; a task OS lock serializes supported writers. SQL CAS selects the verified complete generation. | A file write can fail; the process can die after artifact fsync or SQL commit but before checkpoint. External processes can ignore advisory locks. | Partial stages never change the pointer. Journals classify incomplete/orphan/committed/corrupt without adopting or deleting data. Exact current-operation replay reuses its generation. Old-parent evidence, approval and final result writes fail. | Filesystem and SQL are separate commits. Arbitrary administrator/same-identity tampering is outside the lock guarantee. General queue recovery, Worker fencing and retention remain CH-04/CH-16. |
| Parallel branches | LangGraph joins `researcher` and `test_analyst` before `coder`. Research uses the bound immutable generation. | Another accepted generation can supersede an old graph while it is computing. | Node checks and the receiver-side generation CAS reject stale observations/results; retry inspection uses the bound artifact. | These checks do not establish a Worker ownership epoch or fence provider/GitHub requests. |
| Read/write/create capability | The trusted `workspace-capabilities-v1` policy filters inspection, derives `editable_paths`, and is checked again inside `apply_edits()`. Tests and build/CI files are readable but read-only by default; credential-like files are not readable; create is default-deny. | A model can still request another path, a read-only test, an alias, or a new file. A trusted adapter can deliberately grant a narrower project-specific override. | Stable `CapabilityError` reasons reject the whole batch. Path normalization, case-folded identity, symlink/hard-link/special-file checks, extension/directory/size limits, and expected-absent checks occur before mutation. | This is an application-level policy, not an OS policy plane. Overrides are currently injected by trusted Python configuration rather than a signed project manifest. |
| Cancellation | SQL persists a request/version; execution-scoped control interrupts model/process/container waits. | Cleanup, control-plane access or remote reconciliation can fail. | Running tasks remain cancelling until owned resources stop; cleanup IDs/errors remain pending. Idle requests use CAS to prevent execution. | Local stop does not retract remote effects or guarantee provider billing stopped; restart does not invent confirmation. |
| Timeouts | Persisted execution budget and separate queue/approval clocks; operation waits cap at remaining budget. | Cleanup may outlast execution; service clock jumps affect persisted wall intervals. | Expiry stops new scheduling, reconciles started model calls as unknown and confirms failed only after cleanup. | No Worker fencing or automatic recovery dispatcher; pending cleanup needs operator inspection. |
| Test sandbox | Docker receives a tar snapshot, disables networking, drops capabilities, sets `no-new-privileges`, resource limits, and runs as UID/GID 10001. Snapshot traversal rejects links/special files and caps entries/bytes. | A container is isolated from mounted host/workspace directories, but Docker containers share the host kernel. The process calls `docker.from_env()`, which uses the local Docker control plane/socket and therefore trusts the parent worker/daemon boundary. | Workspace is copied into a disposable container and removed in `finally`; output is redacted and bounded. | This is not a VM or kernel-level isolation boundary. The code does not implement ActPlane or eBPF policy enforcement, and those capabilities must not be claimed from this sandbox implementation. |
| Candidate and approval | Complete candidate bytes/modes are frozen before tests. `approval-v2` binds request version, candidate and evidence; SQL stores one decision. | Workspace can change; artifacts can be lost/corrupted; opposite decisions can race. | Tests use the artifact copy. API checks integrity and CAS-consumes the request; publisher verifies the approved full tree and fixed parent through a durable intent. | Storage is service-owned; Reviewer risk remains advisory. GitHub verification uses a test transport, not a real remote integration run. |
| Publication | `publish_intents` saves deterministic targets, approved binding and versioned receipt. | HTTP success can lose its response; base/ref/PR can move during requests. | Replay queries exact objects/ref and all-state PR lookup; unknown PR absence never grants a second create. Base movement requires revalidation; conflicts stop writes; receipts survive terminal status. | Single executor per intent; receipt CAS does not fence HTTP. No automatic dispatcher, external transaction, force push or merge. |

## Specific conclusions

The [2026-10-19 CH-02B service probe](ch02b-verification-2026-10-19.md) adds real PostgreSQL
checkpoint/call-ledger and process-exit evidence for policy correction. That probe explicitly
continues the same graph thread after exit; it does not implement automatic queue recovery.
Normal approval-checkpoint recovery was separately exercised by restarting the actual Worker
service, including continuation of a checkpoint made by the pinned pre-CH-02B graph in that earlier
release. CH-03A deliberately rejects those unbound legacy approvals; they require a new task/retest.
Current candidate, evidence, CAS and delivery contracts are described in
[frozen candidates and approval decisions](candidate-approvals.md). Worker restart preserves the
full bound approval object; queue crash recovery and approved-base remote publication remain open.

### Single writer, read-only parallelism

The graph’s parallelism is safe only for the branches as written: `researcher`
reads repository content and `test_analyst` computes metadata. All file mutation
is routed through `apply_policy`, and the worker lock serializes normal task handling.
This should be described as “single graph writer with read-only parallel
analysis,” not as concurrent editing support.

### Complete candidate selection and recovery

The original checkout is retained as the base-blob source. Generated edits only affect a new
private staging tree. Its entire content must equal the parent plus the authorized edit batch;
a complete bundle is fsynced and verified before SQL selects it. Existing readers keep their
bound artifact when the current pointer changes. Evidence, approval and final result writes
validate generation freshness while serialized with generation commits.

Worker startup and task entry reconcile retained journals. A sealed uncommitted artifact is an
orphan, not an accepted result; a SQL-committed operation can be reused after a checkpoint gap.
Reconciliation never enqueues a task, deletes data or overwrites external bytes. See
[candidate generation storage](candidate-generations.md) for ordering, compatibility and operator
action. Arbitrary queue/graph crash recovery remains outside this card.

### Capability policy is trusted configuration, not repository configuration

The default policy is selected when `WorkspaceManager` is built. A service-owned
project adapter may inject another immutable policy (for example, to permit test
maintenance), but Issue text, repository files, graph state, and model output do
not supply or extend policy rules. See [workspace capability policy](workspace-capabilities.md)
for the v1 matrix, rejection reasons, and creation preconditions.

### Redis locking is a lease without fencing

The 900-second TTL bounds abandoned locks, but it also creates a split-brain
window for long tasks: the old worker may continue after expiry while a new
worker acquires the same task. Token equality protects release. Task-state CAS
means only one contender can commit a particular observed state version, but it
does not stop either contender from modifying a workspace or starting an
external side effect first. A renewed lease plus an ownership epoch enforced at
each side effect is still needed for real Worker fencing.

### Provider accounting is durable, not cross-system atomic

The call ledger prevents an unrecorded transport attempt in the controlled path and uses a
conditional reservation to enforce one persisted task cap. It cannot atomically commit with the
remote provider. `RESERVED` after a pre-send crash and `UNKNOWN` after a possibly-sent request are
therefore conservative, consumed states. Missing trusted usage also means the system can enforce
call/output ceilings but cannot promise an exact token or monetary ceiling. See
[model-call budgets and provider failures](model-call-control.md).

### Cancellation and timeout are cooperative and local

SQL stores stop requests and active execution polls them into one control scope. Model waits,
process groups and Docker containers are interrupted; filesystem/SDK threads are joined before
confirmation. Deadlines cap execution and new scheduling, while cleanup may continue until its
bounded checks finish. Cleanup failure or an interrupted predecessor leaves an unconfirmed record.
Publication reconciles possibly sent writes and retains remote IDs or unknown outcomes.
[Execution control](execution-control.md) defines local confirmation; neither provider billing nor
remote rollback follows from it.


### Sandbox claims must remain narrow

The Docker configuration materially reduces ordinary test risk, but it still
uses the host Docker daemon and a shared host kernel. It is therefore accurate
to call it a disposable, network-disabled, capability-reduced container
sandbox. It is not accurate to claim ActPlane, eBPF, VM-grade isolation, or a
kernel-enforced policy plane based on these files.

CH-06A publishes a separate [execution control contract](execution-control.md): task cancellation
and attempt abort have different scopes, and a restarted Worker retains unconfirmed cleanup
evidence rather than killing a possibly reused PID.
