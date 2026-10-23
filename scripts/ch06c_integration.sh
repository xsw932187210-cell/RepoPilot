#!/bin/sh
set -eu

# Reuse the isolated service/fault harness, with a distinct resource prefix.
export REPOPILOT_VERIFICATION_CARD=CH-06C
export CH06A_COMPOSE_PROJECT="${CH06C_COMPOSE_PROJECT:-repopilot_ch06c_$(date +%Y%m%d)_$$}"
export CH06A_API_PORT="${CH06C_API_PORT:-58090}"
export CH06A_DEPENDENCY_IMAGE="${CH06C_DEPENDENCY_IMAGE:-}"
export CH06C_POSTGRES_TEST_URL=postgresql+asyncpg://repopilot:repopilot@postgres:5432/repopilot_ch06c_test
export CH06C_API_URL=http://api:8000
exec ./scripts/ch06a_integration.sh
