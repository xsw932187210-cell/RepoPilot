#!/bin/sh
set -eu

project="${CH03A_COMPOSE_PROJECT:-repopilot_ch03a_$(date +%Y%m%d)_$$}"
port="${CH03A_API_PORT:-58030}"
compose_file="docker-compose.ch03a-test.yml"

case "$project" in
  repopilot_ch03a_*) ;;
  *) printf 'CH-03A requires a dedicated repopilot_ch03a_ project name\n' >&2; exit 1 ;;
esac
case "$project" in
  *[!a-z0-9_-]*) printf 'Invalid CH-03A project name\n' >&2; exit 1 ;;
esac

docker version --format '{{.Server.Version}}'
if [ -n "$(docker ps -a --filter "label=com.docker.compose.project=$project" --format '{{.ID}}')" ] ||
   [ -n "$(docker volume ls --filter "label=com.docker.compose.project=$project" --format '{{.Name}}')" ] ||
   [ -n "$(docker network ls --filter "label=com.docker.compose.project=$project" --format '{{.ID}}')" ]; then
  printf 'Refusing to reuse existing Compose resources: %s\n' "$project" >&2
  exit 1
fi
python3 -c 'import socket,sys; s=socket.socket(); s.bind(("127.0.0.1",int(sys.argv[1]))); s.close()' "$port"

probe_temp_root=$(mktemp -d "${TMPDIR:-/tmp}/repopilot-ch03a.XXXXXX")
CH03A_LEGACY_ROOT="$probe_temp_root/legacy"
mkdir "$CH03A_LEGACY_ROOT"
chmod 755 "$probe_temp_root" "$CH03A_LEGACY_ROOT"
export CH03A_LEGACY_ROOT
export CH03A_API_PORT="$port"
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
    printf 'CH-03A cleanup needs inspection: %s\n' "$project" >&2
    probe_exit=1
  fi
  printf '{"project":"%s","resources_removed":%s}\n' "$project" "$cleanup_verified" \
    >"$report_root/cleanup.json"
  exit "$probe_exit"
}
trap cleanup EXIT INT TERM

# Only the trusted, pinned pre-CH-03A source is exposed to the legacy child.
git archive 12b9e3d src | tar -x -C "$CH03A_LEGACY_ROOT"
printf 'CH-03A project=%s api_port=%s report_dir=%s legacy=12b9e3d\n' "$project" "$port" "$report_root"
if ! compose build api sandbox probe >"$report_root/build.log" 2>&1; then
  tail -n 100 "$report_root/build.log"
  exit 1
fi
compose up -d --no-build postgres redis sandbox migrate api worker

attempt=0
while ! curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 90 ]; then
    printf 'CH-03A API failed its health deadline\n' >&2
    exit 1
  fi
  sleep 1
done

compose exec -T postgres createdb -U repopilot repopilot_ch09_test
compose exec -T postgres createdb -U repopilot repopilot_ch10_test
if ! compose run --rm --no-deps probe python -m pytest \
  --cov-config=tests/coverage.ch03a.ini \
  --cov=repopilot --cov-report=term-missing --cov-fail-under=70 \
  -q -s --junitxml=/workspaces/ch03a-integration.xml >"$report_root/integration.log" 2>&1; then
  cat "$report_root/integration.log"
  exit 1
fi
cat "$report_root/integration.log"
compose cp api:/workspaces/ch03a-integration.xml "$report_root/integration.xml"
compose run --rm --no-deps probe python -m repopilot.evaluation --provider mock \
  --dataset evals/cases.jsonl --output /workspaces/ch03a-mock-evaluation.json --fail-on-regression \
  >"$report_root/mock-run.log"
compose cp api:/workspaces/ch03a-mock-evaluation.json "$report_root/mock-evaluation.json"
compose run --rm --no-deps probe python -c \
  'import importlib.metadata as m,json,sys; print(json.dumps({"python":sys.version.split()[0],"langgraph":m.version("langgraph"),"checkpoint_postgres":m.version("langgraph-checkpoint-postgres"),"pytest":m.version("pytest")},sort_keys=True))' \
  >"$report_root/runtime-versions.json"
compose run --rm --no-deps probe python -c \
  'import hashlib,json,pathlib; roots=[pathlib.Path("src"),pathlib.Path("tests")]; files=sorted(p for root in roots for p in root.rglob("*.py") if "__pycache__" not in p.parts); print(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files},sort_keys=True))' \
  >"$report_root/source-hashes.json"
docker image inspect "$project-app:local" "$project-test:local" "$project-sandbox:local" \
  --format '{{json .Id}}' >"$report_root/image-ids.jsonl"

REPOPILOT_API_URL="http://127.0.0.1:$port" ./scripts/smoke.sh >"$report_root/smoke.jsonl"
REPOPILOT_API_URL="http://127.0.0.1:$port" \
REPOPILOT_COMPOSE_PROJECT="$project" REPOPILOT_COMPOSE_FILE="$compose_file" \
  ./scripts/recovery_smoke.sh >"$report_root/recovery.jsonl"
compose exec -T postgres psql -U repopilot -d repopilot_ch03a_test -Atc 'SHOW server_version' \
  >"$report_root/postgres-version.txt"
compose ps --format json >"$report_root/services.jsonl"
printf 'CH-03A integration, mock eval, API smoke and Worker restart: passed\n'
