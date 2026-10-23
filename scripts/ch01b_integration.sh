#!/bin/sh
set -eu

export REPOPILOT_VERIFICATION_CARD=CH-01B
export CH06A_COMPOSE_PROJECT="${CH01B_COMPOSE_PROJECT:-repopilot_ch01b_$(date +%Y%m%d)_$$}"
export CH06A_API_PORT="${CH01B_API_PORT:-58130}"
export CH06A_DEPENDENCY_IMAGE="${CH01B_DEPENDENCY_IMAGE:-}"
export CH06C_POSTGRES_TEST_URL=postgresql+asyncpg://repopilot:repopilot@postgres:5432/repopilot_ch06c_test
export CH06C_API_URL=http://api:8000
export CH07A_DOCKER_TEST=1
export CH01B_DOCKER_TEST=1
exec ./scripts/ch06a_integration.sh
