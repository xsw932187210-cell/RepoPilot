# Architecture

RepoPilot deliberately separates agentic decisions from deterministic controls.

```mermaid
flowchart LR
  API[FastAPI] --> Q[Redis queue]
  Q --> W[Worker]
  W --> G[LangGraph]
  G --> P[Planner]
  P --> R[Researcher: BM25 + AST symbols]
  P --> T[Test analyst]
  R --> C[Coder]
  T --> C
  C --> S[Docker test runner]
  S --> V[Reviewer]
  V -->|medium/high risk: one bounded remediation| C
  V -->|approved, low-risk, or second candidate| H[HITL interrupt]
  H -->|approved| GH[Governed GitHub tool]
  G --> CP[(PostgreSQL checkpoints)]
  W --> E[(Task and event tables)]
  E --> API
```

## Trust boundaries

1. Repository URLs are limited to public GitHub HTTPS URLs plus one bundled demo URI.
2. Model-generated paths are resolved under the task workspace and may not access `.git`.
3. Test commands are parsed to argument arrays; shell operators and arbitrary executables are rejected.
4. Tests execute from a disposable snapshot of the current task workspace, without inheriting the
   worker's mounts, network access, Linux capabilities, or privilege escalation. Symlinks and
   special files are rejected when the snapshot is built.
5. GitHub writes require a non-empty diff, passing tests, an allowlisted owner, runtime enablement,
   a token, reviewer approval, and a LangGraph human interrupt. Deterministic checks override an
   incorrect model approval and feed their evidence into the bounded retry loop.
6. Tokens and credential-bearing URLs are never placed in graph state or task events.

The worker's Docker-socket mount is an administrative trust boundary: access to that socket is
effectively host-level Docker control. Run RepoPilot only on a trusted host; production deployments
should replace it with a remote, least-privilege sandbox service.

## Persistence and recovery

Every graph node is checkpointed by `AsyncPostgresSaver`. The stable `graph_thread_id` stored on
the task record is the recovery cursor. Approval resumes the same thread with
`Command(resume=...)`; it does not rebuild the workflow from scratch.

When deterministic validation fails, or when the reviewer reports a concrete medium/high-risk
finding, the coder refreshes repository context from the already modified workspace before
applying feedback. A low-risk or evidence-only reviewer concern goes directly to HITL; after one
review-driven remediation, the next candidate is also escalated. Previous edits remain
accumulated in state, and the configured maximum iteration count prevents an unbounded agent loop.

Redis is intentionally not the source of truth. It owns dispatch, short-lived locks,
cancellation flags, and live event fan-out. PostgreSQL owns task history, events, and graph state.

`scripts/recovery_smoke.sh` exercises this contract at the human-approval checkpoint: it creates a
task, waits for the persisted interrupt, restarts the worker, submits the decision, and verifies
that the same task completes without repeating the test runner.

## Repository retrieval

The researcher uses a deterministic hybrid retriever before any code-generation call. It combines
BM25 lexical relevance, path matches, Python AST symbols, and one-hop source/test dependencies.
Every selected file has component scores and match evidence in graph state and task results. A
hard character budget limits model context; selected file contents are included whole rather than
silently truncated, so the coder is not asked to rewrite a file from a partial body.

This is an offline, explainable retrieval baseline. It does not claim embedding search, semantic
reranking, or large-repository quality. See `docs/retrieval.md` for the evaluation contract.

## Observability

Each node records its name, iteration, and duration in graph state. The task result persists these
records, while `GET /api/v1/tasks/{task_id}/metrics` aggregates wall time, node time, node run
counts, per-node duration, iteration count, and sandbox time. Node-update events carry the
corresponding duration so the API, SSE stream, and post-run metrics describe the same execution.
