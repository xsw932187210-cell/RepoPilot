#!/usr/bin/env bash
set -euo pipefail

# Run from the repository root. This is a TRUSTED orchestrator with Docker access.
# Child test containers receive tar snapshots, never these mounts or credentials.
repo_root="$(pwd -P)"
test -f "$repo_root/pyproject.toml"
test -d "$repo_root/reports/real-corpus-cache"
mkdir -p "$repo_root/reports/real-v2"
source_args=(--source thefuck=/sources/thefuck)
for argument in "$@"; do
  case "$argument" in
    --source|--source=*) source_args=(); break ;;
  esac
done
credential_args=()
if test -f "$repo_root/.env"; then
  credential_args=(-v "$repo_root/.env:/app/.env:ro")
fi
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$repo_root/src:/app/src:ro" \
  -v "$repo_root/evals:/app/evals:ro" \
  -v "$repo_root/reports/real-corpus-cache:/sources:ro" \
  -v "$repo_root/reports:/app/reports" \
  "${credential_args[@]}" \
  repopilot-eval-tools:local python -m repopilot.real_evaluation \
    "${source_args[@]}" --output reports/real-v2 "$@"
