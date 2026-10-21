# RepoPilot

RepoPilot is a recoverable, human-governed multi-agent software-delivery system. It accepts a
GitHub issue, researches the repository, proposes a bounded code change, runs tests in an
isolated container, asks a separate reviewer for verification, and checkpoints before any
side-effecting GitHub action.

This is an independent learning and portfolio project inspired by the architectural ideas in
[LangChain Open-SWE](https://github.com/langchain-ai/open-swe). It is intentionally smaller and
self-hosted: the orchestration, persistence, queue, security boundaries, evaluation harness,
and API in this repository were implemented specifically for RepoPilot.

See [Openspec.md](Openspec.md) for the current behavioral contracts, verified evidence,
known limitations, and acceptance criteria for the next iterations (Chinese).

## What it demonstrates

- Explicit LangGraph state machine with Planner, Researcher, Test Analyst, Coder, and Reviewer roles.
- Parallel research/test-analysis fan-out, deterministic-failure retries, one bounded remediation
  for concrete reviewer findings, and reviewer-risk escalation to the human approval gate.
- Single graph writer with a versioned read/write/create policy, full-batch edit preflight,
  read-only tests/build configuration, stale-context rejection, and one bounded Coder correction;
  policy-rejected batches never partially touch the tree.
- Explainable BM25 + Python AST symbol retrieval with source/test dependency expansion and a
  bounded context budget.
- Durable PostgreSQL checkpoints and `Command(resume=...)` human approval.
- Frozen complete candidates, image/command-bound test evidence, versioned one-time approval
  decisions and durable retryable resume intents.
- Private staged proposals, complete artifact generations selected by database CAS, retained
  orphan journals and exact-operation replay after a generation/checkpoint handoff interruption.
- Versioned application-schema migrations, optimistic task-state updates, and explicit result/event
  JSON versions.
- Durable per-task model-call reservations, bounded provider retries/timeouts, conservative
  unknown-result recovery, and separate attempt/outcome metrics.
- Redis dispatch, idempotency lock, cancellation flag, and event fan-out.
- FastAPI task API plus replayable event history and SSE progress.
- Per-node latency, retry, sandbox, and wall-time metrics through a task metrics endpoint.
- Network-disabled Docker test sandbox using a disposable per-task snapshot plus resource,
  capability, command, and path controls.
- Governed GitHub REST tool that can create a draft PR only after a non-empty diff, passing tests,
  separate review, and human approval.
- Ten-scenario offline benchmark and mock model for deterministic CI regression testing.
- Twenty pinned BugsInPy defects from The Fuck with a Docker reproduction validator,
  resumable per-case real-model evaluation, one-shot baseline comparison, and sanitized
  failure/latency reports; this is a one-project pilot, not a broad benchmark claim.
- Automated lint, test, and 70% line-coverage quality gates on every push and pull request.
- OpenAI-compatible model adapter for real repository tasks.

## Quick start

Requirements: Docker Desktop and Docker Compose.

```bash
cp .env.example .env
docker compose up --build -d
./scripts/smoke.sh
./scripts/recovery_smoke.sh
```

The default `MODEL_PROVIDER=mock` requires no API key and repairs the bundled calculator fixture.
Open <http://localhost:8000/docs> for the API.

Compose runs the one-shot application migration before starting API and Worker. Existing deployments
must stop old processes before upgrading and must not mix old/new Workers; see
[database migrations and task state versions](docs/database-migrations.md) for the backup, upgrade,
compatibility, and recovery procedure.

The smoke scripts exercise the stack that is already running. If `.env` currently selects a real
provider, recreate the API and worker with an explicit mock override before collecting deterministic
smoke evidence:

```bash
MODEL_PROVIDER=mock MODEL_NAME=deterministic-mock-v1 OPENAI_API_KEY= OPENAI_BASE_URL= \
  docker compose up --build -d --force-recreate api worker
```

To use a real OpenAI-compatible model, edit `.env`:

```dotenv
MODEL_PROVIDER=openai
MODEL_NAME=gpt-4.1-mini
OPENAI_API_KEY=your-local-secret
# OPENAI_BASE_URL=https://your-compatible-endpoint/v1
MODEL_MAX_CALLS=12
MODEL_REQUEST_TIMEOUT_SECONDS=90
MODEL_MAX_RATE_LIMIT_RETRIES=2
MODEL_MAX_TRANSIENT_RETRIES=2
MODEL_RETRY_BASE_SECONDS=1
MODEL_MAX_RETRY_WAIT_SECONDS=30
MODEL_MAX_TOTAL_BACKOFF_SECONDS=60
MODEL_MAX_TOTAL_TOKENS=0
MODEL_MAX_OUTPUT_TOKENS=4096
```

Never commit `.env`. The file is ignored by Git.

Every online Planner, Coder, policy-correction, Reviewer, and controlled transport retry consumes
the same durable task budget. `MODEL_MAX_TOTAL_TOKENS=0` disables the optional trusted-token gate;
call and output caps still apply. Provider usage is never estimated, so missing usage or special
billing prevents an exact monetary guarantee. See
[model-call budgets and provider failures](docs/model-call-control.md) for error states, recovery,
fallback identity, and deployment rules.

The worker launches short-lived test containers through the mounted Docker socket. Docker Desktop
uses the default `DOCKER_SOCKET_GID=0`; on Linux, set it in `.env` to the value returned by
`stat -c '%g' /var/run/docker.sock`.

## Human approval flow

Create a task:

```bash
curl -X POST http://localhost:8000/api/v1/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "repository_url": "https://github.com/owner/repository",
    "issue_title": "Fix the failing parser edge case",
    "issue_body": "Describe expected behavior and acceptance criteria.",
    "base_branch": "main",
    "test_command": "python -m pytest -q"
  }'
```

When the task reaches `awaiting_approval`, inspect `GET /api/v1/tasks/TASK_ID/approval` for the
frozen candidate, complete manifest, test evidence and Reviewer risk. Fetch the full Diff from
`/api/v1/tasks/TASK_ID/candidates/CANDIDATE_ID/diff`. Then resume the durable thread using the exact
bindings from that view (the helper builds the required `approval-v2` body):

```bash
curl -fsS http://localhost:8000/api/v1/tasks/TASK_ID/approval \
  | python3 scripts/approval_payload.py 'Reviewed full diff and test evidence' \
  | curl -fsS -X POST http://localhost:8000/api/v1/tasks/TASK_ID/approval \
      -H 'Content-Type: application/json' --data-binary @-
```

Legacy boolean-only requests return 422. A committed decision whose Redis delivery fails returns
202 with `saved=true`, `dispatch_state=pending` and a decision ID. Explicitly retry that ID at
`POST /api/v1/tasks/TASK_ID/approval/dispatch`; do not create another approval. Old unbound
checkpoints require a new task and fresh tests. See [candidate and approval contracts](docs/candidate-approvals.md).
Publication uses a durable intent bound to the approved artifact, evidence, decision and base SHA.
It creates deterministic Git objects and a unique branch, and reconciles uncertain responses before
continuing. Ambiguous PR creation never triggers a second blind create. Base movement requires
retesting and approval; cancellation preserves observed remote effects. Receipts remain available at
`GET /api/v1/tasks/{task_id}/publication`. See [publication/recovery](docs/publication.md).
Automatic outbox delivery and Worker fencing remain later change cards.

GitHub writes are off by default. To enable draft-PR creation, set all of the following locally:

```dotenv
GITHUB_WRITE_ENABLED=true
GITHUB_TOKEN=your-fine-grained-token
GITHUB_ALLOWED_OWNERS=your-account-or-organization
```

The token should be fine-grained and limited to the intended repositories. RepoPilot never needs
permission to merge a pull request.

Use a feature branch and pull request for repository changes. The included PR template requires
test, evaluation, recovery, security, and resume-claim evidence before review.

## Evaluation

Run the deterministic ten-scenario workflow regression suite without an LLM key:

```bash
make eval
```

The report includes completion, test pass, exact change scope, HITL, iteration, category, and
latency evidence, plus retrieval Recall@3, Recall@5, and mean reciprocal rank. CI stores the JSON
report as a workflow artifact and fails if any deterministic case regresses. This mock result
measures workflow and retrieval reliability, not LLM coding ability.

Run `make ch02b-integration` for the isolated PostgreSQL/Redis/Docker policy-correction probe,
explicit legacy-checkpoint refusal, API smoke and actual Worker restart. It uses a dedicated
Compose project, fixed Mock configuration and automatic project-scoped cleanup. See the
[CH-02B verification record](docs/ch02b-verification-2026-10-19.md) for results and recovery limits.

Run `make ch03a-integration` to additionally verify bound approvals, previous-head migration,
opposite-decision concurrency, commit/process-exit recovery, real Redis connection failure and
artifact corruption refusal. It also reruns policy probes, mock evaluation and restart smoke.

Run `make ch05a-integration` for staged multi-file failure, retained orphan reconciliation,
generation CAS/replay, and a pinned CH-03A approval upgrade, plus the same full regression and
smoke checks. The dedicated test Compose file pins the previously verified Python base digest;
`CH05A_PYTHON_IMAGE` can select another base explicitly. Source and tests are rebuilt and hashed.
See [generation storage and recovery](docs/candidate-generations.md) for compatibility and limits.
The [2026-10-21 verification record](docs/ch05a-verification-2026-10-21.md) documents the full
229-test service run, source checks, faults and cleanup, including intermediate failures.

After configuring an OpenAI-compatible model in the ignored `.env`, run `make eval-real` for a
separately labelled model-quality report. See [docs/evaluation.md](docs/evaluation.md) for the
evidence contract and rules for defensible resume metrics.

The first complete 20-case paired run is documented in
[docs/real-evaluation-results-2026-09-09.md](docs/real-evaluation-results-2026-09-09.md). It records
the negative result as well as the successes: the current multi-agent path did not beat the
one-shot baseline, so no quality-lift claim is made.

To validate the real corpus without spending model quota, run `make eval-real-reproduce` first.
The real-model protocol uses the same pinned manifest and Docker image identity, resumes completed
case/mode pairs, and keeps quota-pending pairs separate from ordinary failures; see
[docs/real-evaluation-protocol.md](docs/real-evaluation-protocol.md).

Run `make eval-real-retrieval` to measure issue-only target-file ranking on the same commits without
calling a model. Its local JSON report is separate from both repair quality and the mock workflow
regression.

## Development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install ".[dev]"
pytest --cov=repopilot --cov-report=term-missing --cov-fail-under=70 -q
ruff check .
make migration-test
make ch10-migration-test
```

See [docs/architecture.md](docs/architecture.md) for trust boundaries, recovery semantics, and
observability, and [docs/retrieval.md](docs/retrieval.md) for the retrieval scoring and evidence
contract. The [database migration guide](docs/database-migrations.md) defines
application/checkpoint ownership, state transitions, upgrade order, and forward recovery. With the
Compose stack running, `make smoke-recovery` demonstrates worker restart and checkpoint resume at
the human-approval boundary while verifying that the persisted model-call total does not reset.
The [model-call control guide](docs/model-call-control.md) defines the provider request, ledger,
retry, and crash-window contract.

## Current scope

CH-06A adds [confirmed cancellation and persisted task deadlines](docs/execution-control.md).
Running cancellation returns 202/cancelling until owned resource cleanup is confirmed. New tasks
default to 900 seconds of execution; queue and approval time are separately recorded and excluded
unless configured. Run `make ch06a-integration` for the isolated fault/restart suite.


CH-03B's [2026-10-22 verification record](docs/ch03b-verification-2026-10-22.md) covers 281
container tests, publication transport faults, process exits, Mock regression and Worker restart.
Run `make ch03b-integration` for the isolated suite. GitHub itself is simulated; publication uses
the [approved-intent protocol](docs/publication.md), with no automatic merge or Worker fencing.

Version 0.1 focuses on one reliable path: issue to verified change to human approval to optional
draft PR. Slack/Linear triggers, MCP packaging, a web dashboard, multi-language sandbox images,
and broad real-repository benchmarks are deliberately left for later iterations rather than
claimed as done.

## License

MIT
