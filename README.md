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
- Single graph writer with full-batch edit preflight, stale-context rejection, and one bounded
  Coder correction after a tool-policy denial; rejected batches never partially touch the tree.
- Explainable BM25 + Python AST symbol retrieval with source/test dependency expansion and a
  bounded context budget.
- Durable PostgreSQL checkpoints and `Command(resume=...)` human approval.
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
```

Never commit `.env`. The file is ignored by Git.

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

When the task reaches `awaiting_approval`, inspect its result and events, then resume the durable
LangGraph thread:

```bash
curl -X POST http://localhost:8000/api/v1/tasks/TASK_ID/approval \
  -H 'Content-Type: application/json' \
  -d '{"approved": true, "feedback": "Reviewed diff and test evidence"}'
```

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
```

See [docs/architecture.md](docs/architecture.md) for trust boundaries, recovery semantics, and
observability, and [docs/retrieval.md](docs/retrieval.md) for the retrieval scoring and evidence
contract. With the Compose stack running, `make smoke-recovery` demonstrates worker restart and
checkpoint resume at the human-approval boundary.

## Current scope

Version 0.1 focuses on one reliable path: issue to verified change to human approval to optional
draft PR. Slack/Linear triggers, MCP packaging, a web dashboard, multi-language sandbox images,
and broad real-repository benchmarks are deliberately left for later iterations rather than
claimed as done.

## License

MIT
