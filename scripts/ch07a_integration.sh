#!/bin/sh
set -eu

# Reuse the isolated fault harness, enabling all prior service entries and the
# owned synthetic project's two actual Docker evaluation paths.
export REPOPILOT_VERIFICATION_CARD=CH-07A
export CH06A_COMPOSE_PROJECT="${CH07A_COMPOSE_PROJECT:-repopilot_ch07a_$(date +%Y%m%d)_$$}"
export CH06A_API_PORT="${CH07A_API_PORT:-58100}"
export CH06A_DEPENDENCY_IMAGE="${CH07A_DEPENDENCY_IMAGE:-}"
export CH06C_POSTGRES_TEST_URL=postgresql+asyncpg://repopilot:repopilot@postgres:5432/repopilot_ch06c_test
export CH06C_API_URL=http://api:8000
export CH07A_DOCKER_TEST=1
exec ./scripts/ch06a_integration.sh
