FROM python:3.12-slim

ARG DENO_VERSION=v2.4.5
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates \
    && curl -fsSL "https://github.com/denoland/deno/releases/download/${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" -o /tmp/deno.zip \
    && unzip -q /tmp/deno.zip -d /usr/local/bin && rm /tmp/deno.zip && chmod +x /usr/local/bin/deno \
    && apt-get purge -y curl unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY outriggarr ./outriggarr
RUN uv sync --frozen --no-dev
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/config \
    DENO_DIR=/config/.deno \
    OUTRIGGARR_CONFIG_DIR=/config \
    OUTRIGGARR_STAGING_DIR=/staging \
    OUTRIGGARR_PORT=8080

VOLUME ["/config"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn outriggarr.main:app --host 0.0.0.0 --port ${OUTRIGGARR_PORT}"]
