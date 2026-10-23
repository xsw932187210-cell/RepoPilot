#!/bin/sh
# Fresh, credential-free resources; reuse the existing full service fault matrix.
set -eu
export REPOPILOT_VERIFICATION_CARD=CH-18
export CH06A_COMPOSE_PROJECT="repopilot_ch18_$(date +%Y%m%d)_$$"
export CH06A_API_PORT="${CH18_API_PORT:-$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')}"
# Verify the default locked Dockerfile, never a cached dependency image.
export CH06A_DOCKERFILE=Dockerfile CH06A_DEPENDENCY_IMAGE=
export CH06C_POSTGRES_TEST_URL=postgresql+asyncpg://repopilot:repopilot@postgres:5432/repopilot_ch06c_test
export CH06C_API_URL=http://api:8000
export CH07A_DOCKER_TEST=1 CH01B_DOCKER_TEST=1 CH18_VERIFY_NO_SKIPS=1
exec ./scripts/ch06a_integration.sh
