# syntax=docker/dockerfile:1.7

# Multi-stage, uv-based build for the FastAPI app (t2s_api) and the
# conversational CLI (t2s-chat, packages/orchestrator). See docker-compose.yml
# and docker/README.md for how this image is run.
#
# Build for arm64 (dev, Apple Silicon) and amd64 (most deployment targets):
#   docker buildx build --platform linux/amd64,linux/arm64 -t t2s .
# A plain `docker build .` builds for the host platform only, which is enough
# for local development.

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# Stage 1: builder — has uv, resolves + installs dependencies, no toolchain
# baked into anything that ships (this stage itself is discarded at the end).
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

# Official multi-arch uv distribution image; COPY --from picks the binary for
# the target platform automatically under buildx. Pinned to the same version
# CI uses (.github/workflows/ci.yml) -- hardcoded rather than via the
# UV_VERSION ARG because BuildKit does not resolve build-arg substitution in
# an external-image COPY --from reference.
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# ---- dependency layer: manifests only, so `uv sync` here is cached until a
# pyproject.toml or uv.lock actually changes -- editing application source
# below never invalidates it. This is the crux of "layer-cache friendly".
COPY pyproject.toml uv.lock ./
COPY packages/t2s_core/pyproject.toml packages/t2s_core/pyproject.toml
COPY packages/foundation/pyproject.toml packages/foundation/pyproject.toml
COPY packages/api/pyproject.toml packages/api/pyproject.toml
COPY packages/cli/pyproject.toml packages/cli/pyproject.toml
COPY packages/orchestrator/pyproject.toml packages/orchestrator/pyproject.toml

# Third-party dependencies only -- `--no-install-workspace` deliberately
# skips building/installing t2s_core/foundation/api/cli/t2s_nl themselves,
# since their source isn't copied in yet. This is what makes the layer
# reusable across source edits.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --all-packages

# ---- source layer: now install the workspace packages themselves.
COPY packages ./packages

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --all-packages

# Postgres driver for the metadata-store profile (compose `postgres` profile;
# see docker/README.md §Postgres). SQLAlchemy already abstracts the DB URL
# (foundation/db.py), so this is purely an added driver -- no application
# code changes. Installed here, AFTER the last `uv sync`, rather than added
# to any package's pyproject.toml/uv.lock: `uv sync` reconciles the venv to
# exactly match the lockfile on every run, so installing it before a later
# `uv sync` gets it silently uninstalled again (found the hard way -- see
# docker/README.md). Keeping it out of the lockfile also keeps this Docker
# work additive and packages/foundation untouched. psycopg[binary] ships a
# bundled libpq, so no libpq-dev / build toolchain is required.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python "psycopg[binary]>=3.2,<4"

# ---------------------------------------------------------------------------
# Stage 2: runtime — slim, no uv, no compilers, non-root.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

# Non-root user. /data/* are where the named volumes (docker-compose.yml)
# get mounted -- created + chowned here so a *named* volume's first mount
# (which Docker seeds from the image's existing directory) comes up writable
# by `t2s` rather than root.
# sqlite3 is here on purpose and is not a build dependency: it is how a human
# checks the workbench's answer against the sample database by hand, with a
# tool the workbench does not control. `t2s db path <id> --sql "..."` prints
# the exact command. Roughly 1 MB.
RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 t2s \
    && useradd --system --uid 1000 --gid t2s --home-dir /home/t2s --create-home t2s \
    && mkdir -p /data/sample_dbs /data/foundation \
    && chown -R t2s:t2s /data /home/t2s

WORKDIR /app

COPY --from=builder --chown=t2s:t2s /app/.venv /app/.venv
COPY --from=builder --chown=t2s:t2s /app/packages /app/packages

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/t2s \
    T2S_SAMPLE_DB_DIR=/data/sample_dbs \
    T2S_API_DB_URL=sqlite:////data/foundation/foundation.sqlite3 \
    T2S_DB_URL=sqlite:////data/foundation/foundation.sqlite3

# Deliberately NOT setting FIREWORKS_API_KEY here or anywhere above: it must
# arrive at container-start time via `docker run -e` / compose `env_file`,
# never be present in a build arg or layer. Layer-1 CRUD (projects, sessions,
# models, datasets, databases) works with no key at all -- t2s_api.app
# defers constructing a live FireworksClient until a layer-2 request needs
# one -- so the container starts and reports healthy without one.

USER t2s

EXPOSE 8000

VOLUME ["/data/sample_dbs", "/data/foundation"]

# /openapi.json is served unconditionally (FastAPI generates it at import
# time) and needs no database, no key, and no request body -- a real
# liveness+readiness signal rather than a synthetic /health stub.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/openapi.json', timeout=2)" || exit 1

CMD ["uvicorn", "t2s_api.app:app", "--host", "0.0.0.0", "--port", "8000"]
