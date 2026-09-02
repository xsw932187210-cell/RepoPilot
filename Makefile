.PHONY: up down build logs test lint smoke smoke-recovery eval eval-real

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
	docker run --rm -v "$$(pwd)/tests:/app/tests:ro" repopilot:test sh -c 'pip install -q ".[dev]" && python -m pytest --cov=repopilot --cov-report=term-missing --cov-fail-under=70 -q'

lint:
	docker run --rm -v "$$(pwd):/workspace" -w /workspace repopilot-sandbox:local ruff check .

smoke:
	./scripts/smoke.sh

smoke-recovery:
	./scripts/recovery_smoke.sh

eval:
	docker compose run --rm api sh -c 'pip install -q --user ".[dev]" && python -m repopilot.evaluation --provider mock --dataset evals/cases.jsonl --fail-on-regression'

eval-real:
	docker compose run --rm api sh -c 'pip install -q --user ".[dev]" && python -m repopilot.evaluation --provider openai --model "$${MODEL_NAME}" --dataset evals/cases.jsonl'
