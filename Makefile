.PHONY: up down build logs test lint migration-test ch10-migration-test smoke smoke-recovery eval eval-real eval-real-reproduce eval-real-retrieval

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

migration-test:
	./scripts/migration_integration.sh

ch10-migration-test:
	./scripts/ch10_migration_integration.sh

smoke:
	./scripts/smoke.sh

smoke-recovery:
	./scripts/recovery_smoke.sh

eval:
	docker compose run --rm api sh -c 'pip install -q --user ".[dev]" && python -m repopilot.evaluation --provider mock --dataset evals/cases.jsonl --fail-on-regression'

eval-real:
	bash scripts/eval_real.sh

eval-real-reproduce:
	bash scripts/eval_real.sh --reproduce-only

eval-real-retrieval:
	docker run --rm --network none \
		-v "$$(pwd)/src:/app/src:ro" \
		-v "$$(pwd)/scripts/eval_real_retrieval.py:/app/scripts/eval_real_retrieval.py:ro" \
		-v "$$(pwd)/evals:/app/evals:ro" \
		-v "$$(pwd)/reports/real-corpus-cache/thefuck:/source:ro" \
		-v "$$(pwd)/reports:/app/reports" \
		repopilot-eval-tools:local python scripts/eval_real_retrieval.py \
			--source /source --output reports/real-retrieval.json
