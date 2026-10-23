ARG PYTHON_IMAGE=python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements/uv.txt /tmp/uv.txt
RUN pip install --require-hashes -r /tmp/uv.txt
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-install-project --extra oauth --group build
COPY src ./src
COPY examples ./examples
COPY evals ./evals
COPY delivery ./delivery
COPY scripts/check_model_connections.py ./scripts/check_model_connections.py
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-build-isolation --no-editable --extra oauth --group build

RUN useradd --create-home --uid 10001 repopilot \
    && mkdir -p /workspaces \
    && chown -R repopilot:repopilot /app /workspaces

USER repopilot
EXPOSE 8000
CMD ["uvicorn", "repopilot.api:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS integration-test
USER root
COPY tests ./tests
COPY scripts ./scripts
COPY docs/evidence ./docs/evidence
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-build-isolation --no-editable --extra dev --extra oauth --group build
USER repopilot

FROM runtime AS production
