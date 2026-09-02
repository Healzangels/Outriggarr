FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY outriggarr ./outriggarr
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    OUTRIGGARR_CONFIG_DIR=/config \
    OUTRIGGARR_STAGING_DIR=/staging \
    OUTRIGGARR_PORT=8080

VOLUME ["/config", "/staging"]

CMD ["sh", "-c", "uvicorn outriggarr.main:app --host 0.0.0.0 --port ${OUTRIGGARR_PORT}"]
