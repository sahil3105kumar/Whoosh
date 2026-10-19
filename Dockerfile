# syntax=docker/dockerfile:1

# ── Web client: static files ────────────────────────────────────────
FROM node:22-slim AS web
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY apps/web ./
RUN npm run build

# ── Build: resolve the locked dependencies into a venv ──────────────
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first, so code changes don't invalidate this layer
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra server --no-install-project

COPY apps ./apps
COPY packages ./packages
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra server --no-editable

# ── Runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim
RUN useradd --system --uid 10001 --no-create-home whoosh
COPY --from=build /app/.venv /app/.venv
COPY --from=web /web/dist /app/web
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 WHOOSH_WEB_DIR=/app/web
USER whoosh
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

# --workers 1: room state is in memory (docs/decisions.md D9).
# --proxy-headers: rate limiting needs the real client IP from the reverse
#   proxy. Trusting any forwarder is safe only because this port is never
#   published — Caddy is the sole way in (D30).
# --ws-max-size: refuse oversized signaling frames before buffering them (D5).
CMD ["uvicorn", "apps.server.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--ws-max-size", "65536"]
