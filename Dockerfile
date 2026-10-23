FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
COPY README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="cloudflare-dns-autoupdate" \
      org.opencontainers.image.source="https://github.com/moonstar-x/cloudflare-dns-auto-update" \
      org.opencontainers.image.licenses="GPL-3.0"

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates curl dnsutils net-tools \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home app

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    CONFIG_PATH=/config/config.yml

VOLUME ["/config"]
WORKDIR /app
USER app
ENTRYPOINT ["tini", "--", "cloudflare-dns-autoupdate"]
