# syntax=docker/dockerfile:1
#
# One image, every mode. Only the env/command changes:
#   stdio (default):  docker run -i --rm --read-only --network none -v <dir>:/data:ro IMAGE
#   ingest:           docker run --rm --read-only -v <export>:/export:ro -v <dir>:/data IMAGE \
#                       python -m chat_vault.data.ingest --source claude --export /export
#   http (Phase 4):   -e CHAT_VAULT_MODE=http
#
# Base image pinned by digest (multi-arch index: amd64 + arm64). Dependabot bumps it.
ARG PYTHON_IMAGE=python:3.13-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

# ---- build stage: dependencies into a self-contained virtualenv -------------
FROM ${PYTHON_IMAGE} AS build
# Generous timeout/retries so a flaky network doesn't fail the build.
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_DEFAULT_TIMEOUT=60 PIP_RETRIES=10
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
# --require-hashes: every package must match the sha256 recorded in the lockfile,
# so a tampered or swapped package fails the build. Wheels only, no source builds.
RUN /opt/venv/bin/pip install --require-hashes --only-binary=:all: -r /tmp/requirements.txt \
 && /opt/venv/bin/pip uninstall --yes pip

# ---- runtime stage --------------------------------------------------------------
FROM ${PYTHON_IMAGE}

# Remove the base image's pip (not needed at runtime) and add an unprivileged user.
RUN python -m pip uninstall --yes pip \
 && groupadd --system --gid 10001 chatvault \
 && useradd --system --uid 10001 --gid 10001 --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin chatvault

COPY --from=build /opt/venv /opt/venv
# Application code is owned by root and therefore not writable by the app user.
COPY chat_vault /app/chat_vault

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHAT_VAULT_MODE=stdio \
    CHAT_VAULT_DB_PATH=/data/chat-vault.db

# Numeric UID so "must run as non-root" checks can verify it.
USER 10001:10001
WORKDIR /data

CMD ["python", "-m", "chat_vault.server"]
