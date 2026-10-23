#!/bin/sh
set -eu

api_url="${REPOPILOT_API_URL:-http://localhost:8000}"
compose_file="${REPOPILOT_COMPOSE_FILE:-docker-compose.yml}"
compose_project="${REPOPILOT_COMPOSE_PROJECT:-}"
strategy="${REPOPILOT_EXECUTION_STRATEGY:-workflow}"
case "$strategy" in
  workflow) expected_minimum=3 ;;
  oneshot|auto) expected_minimum=1 ;;
  *) printf 'Unsupported recovery strategy\n' >&2; exit 1 ;;
esac
create_payload=$(python3 -c 'import json,sys; print(json.dumps({"repository_url":"demo://buggy-calculator","issue_title":"Fix add returning the wrong result","issue_body":"The add function subtracts instead of adding. Make the smallest safe correction.","test_command":"python -m pytest -q","execution_strategy":sys.argv[1]}))' "$strategy")

task_json=$(curl -fsS -X POST "$api_url/api/v1/tasks" \
  -H 'Content-Type: application/json' \
  -d "$create_payload")

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
before_calls=$(printf '%s' "$before_metrics" | python3 -c 'import json,sys; data=json.load(sys.stdin); assert data["model_calls_reserved"] >= int(sys.argv[1]); assert data["model_calls_pending_reservation"] == data["model_calls_pending_response"] == 0; print(data["model_calls_reserved"])' "$expected_minimum")

printf 'Restarting the worker at the durable approval checkpoint\n'
before_approval=$(curl -fsS "$api_url/api/v1/tasks/$task_id/approval")
if [ -n "$compose_project" ]; then
  docker compose -p "$compose_project" -f "$compose_file" restart worker >/dev/null
else
  docker compose -f "$compose_file" restart worker >/dev/null
fi

after_approval=$(curl -fsS "$api_url/api/v1/tasks/$task_id/approval")
[ "$before_approval" = "$after_approval" ] || { printf 'Approval binding changed after restart\n' >&2; exit 1; }
approval_payload=$(printf '%s' "$after_approval" | python3 scripts/approval_payload.py 'worker restart approval')
printf 'Approval object, complete manifest and test evidence unchanged after restart\n'
curl -fsS -X POST "$api_url/api/v1/tasks/$task_id/approval" \
  -H 'Content-Type: application/json' \
  -d "$approval_payload" >/dev/null

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
