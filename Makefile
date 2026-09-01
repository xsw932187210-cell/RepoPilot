.PHONY: up down build logs test lint smoke eval

up:
	docker compose up --build -d

down:
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f api worker

test:
	docker build -f Dockerfile --target runtime -t repopilot:test .
	docker run --rm -v "$$(pwd)/tests:/app/tests:ro" repopilot:test sh -c 'pip install -q ".[dev]" && python -m pytest -q'

lint:
	docker run --rm -v "$$(pwd):/workspace" -w /workspace repopilot-sandbox:local ruff check .

smoke:
	./scripts/smoke.sh

eval:
	docker compose run --rm api sh -c 'pip install -q --user ".[dev]" && python -m repopilot.evaluation --dataset evals/cases.jsonl'
