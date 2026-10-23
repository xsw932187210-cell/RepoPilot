ARG PYTHON_IMAGE=python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/opt/sandbox/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/sandbox
COPY requirements/uv.txt /tmp/uv.txt
RUN pip install --require-hashes -r /tmp/uv.txt
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --only-group sandbox

RUN useradd --create-home --uid 10002 sandbox
USER sandbox
WORKDIR /workspace
ENTRYPOINT []
CMD ["python", "-m", "pytest", "-q"]
