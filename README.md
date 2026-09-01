# RepoPilot

RepoPilot is a recoverable, human-governed multi-agent software-delivery system. It accepts a
GitHub issue, researches the repository, proposes a bounded code change, runs tests in an
isolated container, asks a separate reviewer for verification, and checkpoints before any
side-effecting GitHub action.

This is an independent learning and portfolio project inspired by the architectural ideas in
[LangChain Open-SWE](https://github.com/langchain-ai/open-swe). It is intentionally smaller and
self-hosted: the orchestration, persistence, queue, security boundaries, evaluation harness,
and API in this repository were implemented specifically for RepoPilot.

## What it demonstrates

- Explicit LangGraph state machine with Planner, Researcher, Test Analyst, Coder, and Reviewer roles.
- Parallel research/test-analysis fan-out and a bounded reviewer feedback loop.
- Durable PostgreSQL checkpoints and `Command(resume=...)` human approval.
- Redis dispatch, idempotency lock, cancellation flag, and event fan-out.
- FastAPI task API plus replayable event history and SSE progress.
- Network-disabled Docker test sandbox with resource, capability, command, and path controls.
- Governed GitHub REST tool that can create a draft PR only after tests, review, and approval.
- Offline mock model and fixture repository for deterministic CI and evaluation.
- OpenAI-compatible model adapter for real repository tasks.

## Quick start

Requirements: Docker Desktop and Docker Compose.

```bash
cp .env.example .env
docker compose up --build -d
./scripts/smoke.sh
```

The default `MODEL_PROVIDER=mock` requires no API key and repairs the bundled calculator fixture.
Open <http://localhost:8000/docs> for the API.

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

## Evaluation

Run the offline dataset without an LLM key:

```bash
docker compose run --rm api sh -c \
  'pip install -q --user ".[dev]" && python -m repopilot.evaluation --dataset evals/cases.jsonl'
```

The report includes task success, test pass, HITL, iteration, and latency evidence. Add realistic
issues with hidden tests before quoting any metric in a resume.

## Development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install ".[dev]"
pytest -q
ruff check .
```

See [docs/architecture.md](docs/architecture.md) for trust boundaries and recovery semantics.

## Current scope

Version 0.1 focuses on one reliable path: issue to verified change to human approval to optional
draft PR. Slack/Linear triggers, MCP packaging, a web dashboard, multi-language sandbox images,
and benchmark expansion are deliberately left for later iterations rather than claimed as done.

## License

MIT
