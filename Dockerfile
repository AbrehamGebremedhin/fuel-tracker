# syntax=docker/dockerfile:1

FROM python:3.12-slim

# uv binary (pinned to match local toolchain).
COPY --from=ghcr.io/astral-sh/uv:0.8.18 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH" \
    FUEL_TRACKER_DB=/data/fuel_tracker.db

WORKDIR /app

# 1) Install dependencies first for layer caching (no project code yet).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# 2) Copy the source and install the project itself.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# /data holds the sqlite DB (local volume or a mounted Render disk).
# Runs as root so writes to a root-owned mounted persistent disk succeed.
RUN mkdir -p /data
VOLUME ["/data"]

# Polling bot — no ports to expose.
CMD ["fuel-tracker"]
