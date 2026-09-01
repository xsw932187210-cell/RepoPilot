FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY examples ./examples
COPY evals ./evals
RUN pip install --upgrade pip && pip install "."

RUN useradd --create-home --uid 10001 repopilot \
    && mkdir -p /workspaces \
    && chown -R repopilot:repopilot /app /workspaces

USER repopilot
EXPOSE 8000
CMD ["uvicorn", "repopilot.api:app", "--host", "0.0.0.0", "--port", "8000"]
