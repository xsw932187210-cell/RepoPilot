#!/bin/sh
set -eu

api_url="${REPOPILOT_API_URL:-http://localhost:8000}"

task_json=$(curl -fsS -X POST "$api_url/api/v1/tasks" \
  -H 'Content-Type: application/json' \
  -d '{"repository_url":"demo://buggy-calculator","issue_title":"Fix add returning the wrong result","issue_body":"The add function subtracts instead of adding. Make the smallest safe correction.","test_command":"python -m pytest -q"}')

task_id=$(printf '%s' "$task_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
printf 'Created task %s\n' "$task_id"

attempt=0
while [ "$attempt" -lt 60 ]; do
  status=$(curl -fsS "$api_url/api/v1/tasks/$task_id" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  if [ "$status" = "awaiting_approval" ]; then
    break
  fi
  if [ "$status" = "failed" ] || [ "$status" = "cancelled" ]; then
    printf 'Task stopped before approval: %s\n' "$status" >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 1
done

curl -fsS -X POST "$api_url/api/v1/tasks/$task_id/approval" \
  -H 'Content-Type: application/json' \
  -d '{"approved":true,"feedback":"smoke test approval"}' >/dev/null

attempt=0
while [ "$attempt" -lt 60 ]; do
  task_json=$(curl -fsS "$api_url/api/v1/tasks/$task_id")
  status=$(printf '%s' "$task_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  if [ "$status" = "completed" ]; then
    metrics_json=$(curl -fsS "$api_url/api/v1/tasks/$task_id/metrics")
    printf '%s' "$metrics_json" | python3 -c 'import json,sys; data=json.load(sys.stdin); assert data["status"] == "completed"; assert data["node_runs"].get("test_runner") == 1; assert data["model_calls_reserved"] >= 3; assert data["model_calls_reserved"] == data["model_calls_started"] == data["model_calls_succeeded"]; assert data["model_calls_failed"] == data["model_calls_unknown"] == 0'
    printf '%s\n' "$task_json"
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

printf 'Timed out waiting for task completion\n' >&2
exit 1
