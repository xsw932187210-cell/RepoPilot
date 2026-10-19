#!/bin/sh
set -eu

project="${CH02B_COMPOSE_PROJECT:-repopilot_ch02b_$(date +%Y%m%d)_$$}"
port="${CH02B_API_PORT:-58020}"
compose_file="docker-compose.ch02b-test.yml"

case "$project" in
  repopilot_ch02b_*) ;;
  *) printf 'CH-02B requires a dedicated repopilot_ch02b_ project name\n' >&2; exit 1 ;;
esac
case "$project" in
  *[!a-z0-9_-]*) printf 'Invalid CH-02B project name\n' >&2; exit 1 ;;
esac

docker version --format '{{.Server.Version}}'
if [ -n "$(docker ps -a --filter "label=com.docker.compose.project=$project" --format '{{.ID}}')" ] ||
   [ -n "$(docker volume ls --filter "label=com.docker.compose.project=$project" --format '{{.Name}}')" ] ||
   [ -n "$(docker network ls --filter "label=com.docker.compose.project=$project" --format '{{.ID}}')" ]; then
  printf 'Refusing to reuse existing Compose resources: %s\n' "$project" >&2
  exit 1
fi
python3 -c 'import socket,sys; s=socket.socket(); s.bind(("127.0.0.1",int(sys.argv[1]))); s.close()' "$port"

probe_temp_root=$(mktemp -d "${TMPDIR:-/tmp}/repopilot-ch02b.XXXXXX")
CH02B_LEGACY_ROOT="$probe_temp_root/legacy"
mkdir "$CH02B_LEGACY_ROOT"
chmod 755 "$probe_temp_root" "$CH02B_LEGACY_ROOT"
export CH02B_LEGACY_ROOT
export CH02B_API_PORT="$port"
export COMPOSE_PROJECT_NAME="$project"
report_root="reports/$project"
mkdir -p "$report_root"

compose() {
  docker compose --env-file /dev/null -p "$project" -f "$compose_file" "$@"
}

cleanup() {
  probe_exit=$?
  trap - EXIT INT TERM
  cleanup_verified=false
  if compose down -v --remove-orphans &&
     remaining_containers=$(docker ps -aq --filter "label=com.docker.compose.project=$project") &&
     remaining_volumes=$(docker volume ls -q --filter "label=com.docker.compose.project=$project") &&
     remaining_networks=$(docker network ls -q --filter "label=com.docker.compose.project=$project") &&
     [ -z "$remaining_containers$remaining_volumes$remaining_networks" ]; then
    cleanup_verified=true
    rm -rf "$probe_temp_root"
  else
    printf 'CH-02B cleanup needs inspection: %s\n' "$project" >&2
    probe_exit=1
  fi
  printf '{"project":"%s","resources_removed":%s}\n' "$project" "$cleanup_verified" \
    >"$report_root/cleanup.json"
  exit "$probe_exit"
}
trap cleanup EXIT INT TERM

# Only the trusted, pinned pre-CH-02B source is exposed to the legacy child.
git archive 12b9e3d src | tar -x -C "$CH02B_LEGACY_ROOT"
printf 'CH-02B project=%s api_port=%s report_dir=%s legacy=12b9e3d\n' "$project" "$port" "$report_root"
compose build api sandbox probe
compose up -d --no-build postgres redis sandbox migrate api worker

attempt=0
while ! curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 90 ]; then
    printf 'CH-02B API failed its health deadline\n' >&2
    exit 1
  fi
  sleep 1
done

compose run --rm --no-deps probe python -m pytest tests/test_policy_recovery_postgres.py \
  -q -s --junitxml=/workspaces/ch02b-integration.xml
compose cp api:/workspaces/ch02b-integration.xml "$report_root/integration.xml"
compose run --rm --no-deps probe python -m repopilot.evaluation --provider mock \
  --dataset evals/cases.jsonl --output /workspaces/ch02b-mock-evaluation.json --fail-on-regression \
  >"$report_root/mock-run.log"
compose cp api:/workspaces/ch02b-mock-evaluation.json "$report_root/mock-evaluation.json"
compose run --rm --no-deps probe python -c \
  'import importlib.metadata as m,json,sys; print(json.dumps({"python":sys.version.split()[0],"langgraph":m.version("langgraph"),"checkpoint_postgres":m.version("langgraph-checkpoint-postgres"),"pytest":m.version("pytest")},sort_keys=True))' \
  >"$report_root/runtime-versions.json"
docker image inspect "$project-app:local" "$project-test:local" "$project-sandbox:local" \
  --format '{{json .Id}}' >"$report_root/image-ids.jsonl"

REPOPILOT_API_URL="http://127.0.0.1:$port" ./scripts/smoke.sh >"$report_root/smoke.jsonl"
REPOPILOT_API_URL="http://127.0.0.1:$port" \
REPOPILOT_COMPOSE_PROJECT="$project" REPOPILOT_COMPOSE_FILE="$compose_file" \
  ./scripts/recovery_smoke.sh >"$report_root/recovery.jsonl"
compose exec -T postgres psql -U repopilot -d repopilot_ch02b_test -Atc 'SHOW server_version' \
  >"$report_root/postgres-version.txt"
compose ps --format json >"$report_root/services.jsonl"
printf 'CH-02B integration, mock eval, API smoke and Worker restart: passed\n'
