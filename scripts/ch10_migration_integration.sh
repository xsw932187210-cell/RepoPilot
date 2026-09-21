#!/bin/sh
set -eu

project="${CH10_COMPOSE_PROJECT:-repopilot_ch10_$$}"
port="${CH10_POSTGRES_PORT:-55440}"
compose_file="docker-compose.ch10-test.yml"

if [ -n "$(docker ps -a --filter "label=com.docker.compose.project=$project" --format '{{.ID}}')" ]; then
  echo "Refusing to reuse existing Compose project: $project" >&2
  exit 1
fi
if [ -n "$(docker ps --filter "publish=$port" --format '{{.ID}}')" ]; then
  echo "Refusing to use occupied PostgreSQL test port: $port" >&2
  exit 1
fi

export CH10_POSTGRES_PORT="$port"

cleanup() {
  docker compose -p "$project" -f "$compose_file" down -v --remove-orphans
}

trap cleanup EXIT INT TERM
echo "CH-10 PostgreSQL probe: project=$project port=$port volume=${project}_postgres-data"
docker compose -p "$project" -f "$compose_file" up \
  --build \
  --abort-on-container-exit \
  --exit-code-from migration-test \
  migration-test
