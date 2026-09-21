#!/bin/sh
set -eu

api_url="${REPOPILOT_API_URL:-http://localhost:8000}"
compose_file="${REPOPILOT_COMPOSE_FILE:-docker-compose.yml}"
compose_project="${REPOPILOT_COMPOSE_PROJECT:-}"

task_json=$(curl -fsS -X POST "$api_url/api/v1/tasks" \
  -H 'Content-Type: application/json' \
  -d '{"repository_url":"demo://buggy-calculator","issue_title":"Fix add returning the wrong result","issue_body":"The add function subtracts instead of adding. Make the smallest safe correction.","test_command":"python -m pytest -q"}')

task_id=$(printf '%s' "$task_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
printf 'Created recovery task %s\n' "$task_id"

attempt=0
while [ "$attempt" -lt 60 ]; do
  status=$(curl -fsS "$api_url/api/v1/tasks/$task_id" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  if [ "$status" = "awaiting_approval" ]; then
    break
  fi
  if [ "$status" = "failed" ] || [ "$status" = "cancelled" ]; then
    printf 'Task stopped before checkpoint: %s\n' "$status" >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 1
done

if [ "$status" != "awaiting_approval" ]; then
  printf 'Timed out waiting for the approval checkpoint\n' >&2
  exit 1
fi

before_metrics=$(curl -fsS "$api_url/api/v1/tasks/$task_id/metrics")
before_calls=$(printf '%s' "$before_metrics" | python3 -c 'import json,sys; data=json.load(sys.stdin); assert data["model_calls_reserved"] >= 3; assert data["model_calls_pending_reservation"] == data["model_calls_pending_response"] == 0; print(data["model_calls_reserved"])')

printf 'Restarting the worker at the durable approval checkpoint\n'
if [ -n "$compose_project" ]; then
  docker compose -p "$compose_project" -f "$compose_file" restart worker >/dev/null
else
  docker compose -f "$compose_file" restart worker >/dev/null
fi

curl -fsS -X POST "$api_url/api/v1/tasks/$task_id/approval" \
  -H 'Content-Type: application/json' \
  -d '{"approved":true,"feedback":"recovery smoke approval after worker restart"}' >/dev/null

attempt=0
while [ "$attempt" -lt 60 ]; do
  task_json=$(curl -fsS "$api_url/api/v1/tasks/$task_id")
  status=$(printf '%s' "$task_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  if [ "$status" = "completed" ]; then
    metrics_json=$(curl -fsS "$api_url/api/v1/tasks/$task_id/metrics")
    printf '%s' "$metrics_json" | python3 -c 'import json,sys; data=json.load(sys.stdin); expected=int(sys.argv[1]); assert data["status"] == "completed"; assert data["node_runs"].get("approval") == 1; assert data["node_runs"].get("finalize") == 1; assert data["model_calls_reserved"] == expected; assert data["model_calls_succeeded"] == expected; assert data["model_calls_unknown"] == 0' "$before_calls"
    printf 'Recovered task %s after worker restart\n' "$task_id"
    printf '%s\n' "$metrics_json"
    exit 0
  fi
  if [ "$status" = "failed" ] || [ "$status" = "cancelled" ]; then
    printf '%s\n' "$task_json" >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 1
done

printf 'Timed out waiting for recovered task completion\n' >&2
exit 1
