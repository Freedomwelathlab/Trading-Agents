# Trading OS API image (Phase 41 hardening — docs/DECISIONS.md D054).
#
# Two stages and a non-root runtime user. Before this phase the image was a
# single stage that ran `pip install` in the same layer set it shipped, and
# ran the app as root.
#
# ---------------------------------------------------------------------------
# Stage 1: builder — may carry compilers, caches, and pip's own machinery.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Dependencies are resolved from pyproject.toml alone, before any source is
# copied, so an edit to application code does not invalidate the (slow)
# dependency layer. `--no-deps` on the second install is what keeps the
# app-code copy from re-resolving the whole tree.
# The `pip install .` → `pip uninstall trading-os` pair is the whole trick:
# installing the project is how pyproject.toml's dependency list gets
# resolved (no separate requirements file exists, and inventing one would be
# a second source of truth), and uninstalling it immediately afterwards
# leaves the dependencies while dropping the *copy* of our own source that
# `pip install .` put in site-packages. Without the uninstall the image
# carries two copies of `apps/` and `packages/` — the one in the venv and
# the one at /app — and which of them a given import resolves to is decided
# by sys.path ordering, which is exactly the kind of question a production
# image should not have. The app code is served from /app via PYTHONPATH
# below, one copy, unambiguously.
COPY pyproject.toml ./
RUN mkdir -p apps packages \
    && python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install . \
    && /opt/venv/bin/pip uninstall -y trading-os \
    && /opt/venv/bin/pip uninstall -y setuptools wheel \
    && /opt/venv/bin/pip uninstall -y pip

# pip, setuptools, and wheel are build-time tools; nothing at runtime
# imports them and nothing in the runtime stage installs anything. Removing
# them from the venv before it is copied forward is the multi-stage split
# doing its actual job — it takes ~19MB, and it takes an installer capable
# of fetching and executing arbitrary code off the network out of the
# shipped image. (The base image's own /usr/local system pip is removed
# separately, in the runtime stage — this line only clears the venv's copy.)
# `pip uninstall pip` must come last, for obvious reasons.

# Everything runs out of a self-contained venv rather than the system
# site-packages: a venv is a single directory that can be copied whole into
# the runtime stage, which is exactly what makes the multi-stage split
# straightforward here. No new tool is involved — `venv` and `pip` are both
# already in the base image.

# ---------------------------------------------------------------------------
# Stage 2: runtime — no build tooling, no pip cache, no root.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Created before anything is copied so the COPY --chown targets exist.
# The base image ships its own system pip/setuptools under /usr/local
# independently of the venv; it is dropped here for the same reason the
# venv's copy is dropped in the builder. Everything this app runs lives in
# /opt/venv, so nothing at runtime needs either.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && pip uninstall -y pip setuptools wheel 2>/dev/null || true

WORKDIR /app

COPY --from=builder --chown=appuser:appuser /opt/venv /opt/venv

# Application code, the package `pyproject.toml` names, and the migration
# set. `alembic.ini` + `migrations/` are shipped deliberately: nothing in
# this image's own CMD runs migrations (see docker-compose.yml — the `api`
# service starts uvicorn directly and expects `alembic upgrade head` to have
# been run against the database out-of-band, per
# docs/DEVELOPMENT_WORKFLOW.md), but shipping them keeps
# `docker compose run --rm api alembic upgrade head` a working, one-command
# path from the same image that serves traffic. Dropping them would save a
# few kilobytes and remove the only in-image way to migrate.
COPY --chown=appuser:appuser pyproject.toml ./
COPY --chown=appuser:appuser apps/api ./apps/api
COPY --chown=appuser:appuser packages ./packages
COPY --chown=appuser:appuser migrations ./migrations
COPY --chown=appuser:appuser alembic.ini ./

# The single copy of the application code is the tree at /app (the builder
# uninstalled the site-packages copy — see above). Putting /app on the path
# makes `apps.api.app.main` resolve to it: the same import target the
# previous single-stage image's editable install produced, without needing
# pip, setuptools, or a build backend at runtime.
ENV PYTHONPATH=/app

USER appuser

EXPOSE 8000

# Unchanged from the single-stage image: same module path, same host, same
# port, same env-var contract (DATABASE_URL / REDIS_URL / JWT_SECRET_KEY are
# still read from the environment by apps/api/app/core/config.py).
CMD ["uvicorn", "apps.api.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
