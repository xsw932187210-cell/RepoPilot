#!/bin/sh
set -eu

verification_card="${REPOPILOT_VERIFICATION_CARD:-CH-06A}"
case "$verification_card" in
  CH-06A) project_prefix=repopilot_ch06a_ ;;
  CH-06C) project_prefix=repopilot_ch06c_ ;;
  CH-07A) project_prefix=repopilot_ch07a_ ;;
  CH-07C) project_prefix=repopilot_ch07c_ ;;
  CH-01B) project_prefix=repopilot_ch01b_ ;;
  CH-18) project_prefix=repopilot_ch18_ ;;
  *) printf 'Unsupported verification card\n' >&2; exit 1 ;;
esac
project="${CH06A_COMPOSE_PROJECT:-${project_prefix}$(date +%Y%m%d)_$$}"
port="${CH06A_API_PORT:-58080}"
compose_file="docker-compose.ch06a-test.yml"

case "$project" in
  "$project_prefix"*) ;;
  *) printf '%s requires a dedicated %s project name\n' "$verification_card" "$project_prefix" >&2
     exit 1 ;;
esac
case "$project" in
  *[!a-z0-9_-]*) printf 'Invalid CH-06A project name\n' >&2; exit 1 ;;
esac

docker version --format '{{.Server.Version}}'
if [ -n "$(docker ps -a --filter "label=com.docker.compose.project=$project" --format '{{.ID}}')" ] ||
   [ -n "$(docker volume ls --filter "label=com.docker.compose.project=$project" --format '{{.Name}}')" ] ||
   [ -n "$(docker network ls --filter "label=com.docker.compose.project=$project" --format '{{.ID}}')" ]; then
  printf 'Refusing to reuse existing Compose resources: %s\n' "$project" >&2
  exit 1
fi
python3 -c 'import socket,sys; s=socket.socket(); s.bind(("127.0.0.1",int(sys.argv[1]))); s.close()' "$port"

probe_temp_root=$(mktemp -d "${TMPDIR:-/tmp}/repopilot-ch06a.XXXXXX")
CH06A_LEGACY_ROOT="$probe_temp_root/legacy"
CH06A_APPROVAL_LEGACY_ROOT="$probe_temp_root/approval-legacy"
mkdir "$CH06A_LEGACY_ROOT" "$CH06A_APPROVAL_LEGACY_ROOT"
chmod 755 "$probe_temp_root" "$CH06A_LEGACY_ROOT" "$CH06A_APPROVAL_LEGACY_ROOT"
export CH06A_LEGACY_ROOT CH06A_APPROVAL_LEGACY_ROOT
export CH06A_API_PORT="$port"
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
    printf 'CH-06A cleanup needs inspection: %s\n' "$project" >&2
    probe_exit=1
  fi
  printf '{"project":"%s","resources_removed":%s}\n' "$project" "$cleanup_verified" \
    >"$report_root/cleanup.json"
  if [ "$verification_card" = CH-18 ]; then
    python3 scripts/verification_summary.py "$report_root" || probe_exit=1
  fi
  exit "$probe_exit"
}
trap cleanup EXIT INT TERM

if [ -n "${CH06A_DEPENDENCY_IMAGE:-}" ]; then
  export CH06A_DOCKERFILE=Dockerfile.ch06a-cached
  docker image inspect "$CH06A_DEPENDENCY_IMAGE" --format '{{json .Id}}' \
    >"$report_root/dependency-image-id.json"
fi

# Only the trusted, pinned pre-CH-06A source is exposed to the legacy child.
git archive 12b9e3d src | tar -x -C "$CH06A_LEGACY_ROOT"
git archive a1e5f41 src | tar -x -C "$CH06A_APPROVAL_LEGACY_ROOT"
printf '%s project=%s api_port=%s report_dir=%s legacy=12b9e3d,approval=a1e5f41\n' \
  "$verification_card" "$project" "$port" "$report_root"
if [ "$verification_card" = CH-18 ]; then
  python3 scripts/verification_summary.py "$report_root" --snapshot
fi
if ! compose build api sandbox probe >"$report_root/build.log" 2>&1; then
  tail -n 100 "$report_root/build.log"
  exit 1
fi
compose up -d --no-build postgres redis sandbox migrate api worker

attempt=0
while ! curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 90 ]; then
    printf 'CH-06A API failed its health deadline\n' >&2
    exit 1
  fi
  sleep 1
done

if [ "$verification_card" = CH-18 ]; then
  compose run --rm --no-deps probe repopilot-doctor >"$report_root/doctor.json"
fi

compose exec -T postgres createdb -U repopilot repopilot_ch06a_test
compose exec -T postgres createdb -U repopilot repopilot_ch03b_test
compose exec -T postgres createdb -U repopilot repopilot_ch09_test
compose exec -T postgres createdb -U repopilot repopilot_ch10_test
if [ "$verification_card" = CH-06C ] || [ "$verification_card" = CH-07A ] || [ "$verification_card" = CH-07C ] || [ "$verification_card" = CH-01B ] || [ "$verification_card" = CH-18 ]; then
  compose exec -T postgres createdb -U repopilot repopilot_ch06c_test
fi
if ! compose run --rm --no-deps probe python -m pytest \
  --cov-config=tests/coverage.ch06a.ini \
  --cov=repopilot --cov-report=term-missing --cov-fail-under=70 \
  --cov-report=xml:/workspaces/ch06a-coverage.xml \
  -q -s --junitxml=/workspaces/ch06a-integration.xml >"$report_root/integration.log" 2>&1; then
  cat "$report_root/integration.log"
  compose cp api:/workspaces/ch06a-integration.xml "$report_root/integration.xml" || true
  exit 1
fi
cat "$report_root/integration.log"
compose cp api:/workspaces/ch06a-integration.xml "$report_root/integration.xml"
compose cp api:/workspaces/ch06a-coverage.xml "$report_root/coverage.xml"
compose run --rm --no-deps probe python -m repopilot.evaluation --provider mock \
  --dataset evals/cases.jsonl --output /workspaces/ch06a-mock-evaluation.json --fail-on-regression \
  >"$report_root/mock-run.log"
compose cp api:/workspaces/ch06a-mock-evaluation.json "$report_root/mock-evaluation.json"
compose run --rm --no-deps probe python -c \
  'import importlib.metadata as m,json,sys; print(json.dumps({"python":sys.version.split()[0],"langgraph":m.version("langgraph"),"langsmith":m.version("langsmith"),"checkpoint_postgres":m.version("langgraph-checkpoint-postgres"),"pytest":m.version("pytest")},sort_keys=True))' \
  >"$report_root/runtime-versions.json"
compose run --rm --no-deps probe python -c \
  'import hashlib,json,pathlib; roots=[pathlib.Path("src"),pathlib.Path("tests")]; files=sorted(p for root in roots for p in root.rglob("*.py") if "__pycache__" not in p.parts); print(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files},sort_keys=True))' \
  >"$report_root/source-hashes.json"
compose run --rm --no-deps probe python -c \
  'import hashlib,json,pathlib,repopilot; root=pathlib.Path(repopilot.__file__).parent; print(json.dumps({"src/repopilot/"+str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*.py"))},sort_keys=True))' \
  >"$report_root/installed-source-hashes.json"
compose exec -T api python -c \
  'import hashlib,json,pathlib,repopilot; root=pathlib.Path(repopilot.__file__).parent; print(json.dumps({"src/repopilot/"+str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*.py"))},sort_keys=True))' \
  >"$report_root/api-source-hashes.json"
docker image inspect "$project-app:local" "$project-test:local" "$project-sandbox:local" \
  --format '{{json .Id}}' >"$report_root/image-ids.jsonl"

REPOPILOT_API_URL="http://127.0.0.1:$port" ./scripts/smoke.sh >"$report_root/smoke.jsonl"
REPOPILOT_API_URL="http://127.0.0.1:$port" \
REPOPILOT_COMPOSE_PROJECT="$project" REPOPILOT_COMPOSE_FILE="$compose_file" \
  ./scripts/recovery_smoke.sh >"$report_root/recovery.jsonl"
if [ "$verification_card" = CH-01B ] || [ "$verification_card" = CH-18 ]; then
  REPOPILOT_API_URL="http://127.0.0.1:$port" \
  REPOPILOT_COMPOSE_PROJECT="$project" REPOPILOT_COMPOSE_FILE="$compose_file" \
  REPOPILOT_EXECUTION_STRATEGY=oneshot \
    ./scripts/recovery_smoke.sh >"$report_root/recovery-oneshot.jsonl"
fi
compose exec -T postgres psql -U repopilot -d repopilot_ch05a_test -Atc 'SHOW server_version' \
  >"$report_root/postgres-version.txt"
compose ps --format json >"$report_root/services.jsonl"
if [ "$verification_card" = CH-18 ]; then
  printf '{"completed":true}\n' >"$report_root/completed.json"
fi
printf '%s integration, mock eval, API smoke and Worker restart: passed\n' "$verification_card"
