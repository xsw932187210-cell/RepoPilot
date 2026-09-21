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
prepare -> planner -> [researcher, test_analyst] -> coder -> test_runner
                                              reviewer
                                                  |
                                  approval <- bounded remediation / failed
                                      |
                               finalize or cancelled
```

The two branches after `planner` both observe the planner output. They are
read-only with respect to the workspace: `researcher` calls
`WorkspaceManager.inspect()` and `test_analyst` only constructs a test strategy.
`coder` is the sole graph node that calls `apply_edits()`. This makes the graph
single-writer by design, but it does not protect the workspace from processes
outside this graph.

## Mechanism / failure mode / current action / unresolved limitation

| Area | Mechanism in the code | Failure mode or boundary | Current action | Unresolved limitation |
| --- | --- | --- | --- | --- |
| Worker ownership | `JobQueue.acquire()` uses `SET key token NX EX 900`; `release()` deletes only when the token still matches. | Duplicate delivery is skipped while the lock is live. If a run lasts beyond 900 seconds, the key can expire and another worker can acquire the same task while the first worker is still running. | Token-checked release prevents an old worker from deleting a newer owner’s lock. | There is no renewal, lease epoch, or fencing token. The lock is a best-effort lease, not proof of exclusive ownership for the whole run. |
| Queue delivery | Jobs are pushed with `LPUSH` and removed by `BRPOP`. | A worker can die after dequeue and before completion; the job is no longer in the list. A duplicate job can also be enqueued independently. | The task lock and terminal-status check make ordinary duplicate execution mostly harmless. | There is no reliable queue acknowledgement/requeue protocol in this layer; crash recovery depends on external orchestration or another enqueue. |
| Task status | Allowed transitions are explicit. `transition_task()` conditionally matches task ID, expected status, and `state_version`, then updates status/result and increments the version in one SQL statement. | Status writes and graph/Redis/event/workspace side effects are separate operations. A crash between them can leave a task marked `RUNNING` or leave an event/result incomplete. | Illegal edges are rejected; stale writes return a conflict. API races become `409`, and a stale Worker result cannot overwrite a newer terminal task row. | The state version is not an ownership epoch or a recovery watchdog. It does not fence workspace or remote side effects and is not a transaction across the other components. |
| Event publication | `EventBus.publish()` writes the event to the database and then publishes JSON on Redis Pub/Sub. | A database write can succeed while Redis publish fails; a subscriber can disconnect and miss Pub/Sub messages. | Persist-before-publish gives a durable event record before the live notification attempt. | Pub/Sub is not a durable delivery stream and `subscribe()` has no replay cursor in this file. Consumers must use the database separately if they need recovery. |
| Model provider calls | `ControlledModelCaller` atomically reserves one task-wide attempt, commits `STARTED`, then invokes transport with SDK retries disabled. All roles, policy corrections, and controlled retries share the persisted cap. | The reservation/started/outcome transactions and provider request are not one transaction. A process can die before send or after a request may have completed. | A pre-send reservation remains consumed. Recovery converts persisted `STARTED` attempts to `UNKNOWN`; neither state is refunded or silently retried. Stable error classes bound request timeout, retry count, one wait, and cumulative backoff. | This prevents free retries but cannot prevent provider-side duplicate effects or prove exact billing. Task CAS is not Worker fencing; a split-brain Worker remains possible until CH-04B. |
| Context freshness | `inspect()` snapshots selected file contents; `apply_edits()` compares each existing target with `expected_contents` and requires an explicit `expected_absent` precondition for authorized creation. | The compare and subsequent writes are separate. Another writer can change a file after the check. Multiple files can be changed before a later I/O failure. | The complete proposal is validated first; edits outside supplied context, duplicate/aliased paths, oversized files, stale contents, and failed expected-absent checks are rejected before the batch writes. One denial can be returned to Coder for a bounded retry from freshly inspected context. | This is stale-context preflight, not a filesystem transaction or lock against arbitrary writers. There is no atomic multi-file commit or rollback after writes begin. |
| Parallel branches | LangGraph joins `researcher` and `test_analyst` before `coder`. Neither branch edits files. | Their observations are snapshots and can become stale before coding. A repository can also change outside the graph. | `coder` uses the joined research context; on iterations after the first it re-runs `inspect()` before proposing changes. | The first coder pass can use context that is already stale; the re-inspection still does not fence external writers between read and write. |
| Read/write/create capability | The trusted `workspace-capabilities-v1` policy filters inspection, derives `editable_paths`, and is checked again inside `apply_edits()`. Tests and build/CI files are readable but read-only by default; credential-like files are not readable; create is default-deny. | A model can still request another path, a read-only test, an alias, or a new file. A trusted adapter can deliberately grant a narrower project-specific override. | Stable `CapabilityError` reasons reject the whole batch. Path normalization, case-folded identity, symlink/hard-link/special-file checks, extension/directory/size limits, and expected-absent checks occur before mutation. | This is an application-level policy, not an OS policy plane. Overrides are currently injected by trusted Python configuration rather than a signed project manifest. |
| Cancellation | `ensure_active()` checks the Redis cancellation key at node entry. `request_cancel()` sets it for one hour. | Cancellation during an LLM request, repository clone, synchronous inspection, Docker wait, or publisher call is not interruptive. The current node continues until it returns. | Subsequent node boundaries raise `TaskCancelled`; worker maps that to `CANCELLED`. Human rejection at the approval interrupt routes to `cancelled`. | There is no cancellation token propagated into all I/O, no kill of an in-flight Docker container on Redis cancel, and no cleanup/clear operation for the cancel key shown here. |
| Timeouts | `run_process()` uses `wait_for()` and kills the child process group; Docker waits use the configured timeout; each model transport is separately wrapped by `MODEL_REQUEST_TIMEOUT_SECONDS`. | These are per-operation limits, not a whole-task deadline. A model timeout can mean the provider result is unknown, and Docker cleanup can itself fail. | Local timeout returns exit code 124; Docker timeout returns a timed-out `SandboxResult`; model timeout records an `UNKNOWN` attempt before any bounded retry. | End-to-end task deadlines are not enforced. Redis cancellation is not propagated into every active I/O operation. |
| Test sandbox | Docker receives a tar snapshot, disables networking, drops capabilities, sets `no-new-privileges`, resource limits, and runs as UID/GID 10001. Snapshot traversal rejects links/special files and caps entries/bytes. | A container is isolated from mounted host/workspace directories, but Docker containers share the host kernel. The process calls `docker.from_env()`, which uses the local Docker control plane/socket and therefore trusts the parent worker/daemon boundary. | Workspace is copied into a disposable container and removed in `finally`; output is redacted and bounded. | This is not a VM or kernel-level isolation boundary. The code does not implement ActPlane or eBPF policy enforcement, and those capabilities must not be claimed from this sandbox implementation. |
| Publish gate | Passing sandbox result and a non-empty diff reach the approval interrupt; a concrete medium/high-risk model-reviewer finding may request one bounded remediation first. `finalize` runs only after human approval. | Low-risk or evidence-only concerns go directly to HITL; after a second candidate, all remaining reviewer concerns are escalated rather than looping. Human approval is checkpointed, but the workspace/repository may change while paused. | Approval receives changed files, reviewer assessment, test result, and a diff preview; rejection routes to `cancelled`. | Reviewer risk is model-produced and can be wrong. Approval is a workflow gate, not a transaction that binds the reviewed bytes to the eventual publish operation. |

## Specific conclusions

### Single writer, read-only parallelism

The graph’s parallelism is safe only for the branches as written: `researcher`
reads repository content and `test_analyst` computes metadata. All file mutation
is routed through `coder`, and the worker lock serializes normal task handling.
This should be described as “single graph writer with read-only parallel
analysis,” not as concurrent editing support.

### Stale context is detected, not transactionally prevented

`expected_contents` catches an existing file that changed between retrieval and
the preflight read. Authorized creation separately requires `expected_absent`
and uses exclusive create. These are useful optimistic concurrency controls, but
they are not held as a lock through all writes. They do not cover external side
effects, and they cannot roll back earlier writes if a later write fails. The
repository comment correctly calls this a write-boundary check, not a transaction
protocol.

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

Cancellation is observed between graph nodes. Test command timeouts terminate
the child process group/container, but they do not define a global task deadline.
In-flight model, clone, inspection, or publish calls are not interrupted by the
Redis cancel flag in the reviewed code.

### Sandbox claims must remain narrow

The Docker configuration materially reduces ordinary test risk, but it still
uses the host Docker daemon and a shared host kernel. It is therefore accurate
to call it a disposable, network-disabled, capability-reduced container
sandbox. It is not accurate to claim ActPlane, eBPF, VM-grade isolation, or a
kernel-enforced policy plane based on these files.
