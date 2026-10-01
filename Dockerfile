# syntax=docker/dockerfile:1
#
# One image, two programs: the node app (the default) and the RM3100
# simulator (`rm3100_sim serve ...`), so the demo compose file and a node run
# exactly the same bits. Multi-arch: linux/arm64 for the fleet, linux/amd64 for
# CI and laptops. Every dependency is pure Python or ships an arm64 wheel, so
# there is no build stage and no compiler.
#
# Dependencies come from uv.lock, synced with the uv pinned here into a
# virtualenv first on PATH (the org's ADR 2026-09-23-python-apps-lock-with-uv).
# CI reads UV_VERSION from this line; relock with the same version.
ARG UV_VERSION=0.12.5
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:3.12-slim

# Where an image comes from (source, revision, version) is labelled when it is
# built for a release (.github/workflows/release.yml), from the repository and
# the tag that built it, so that it can never name a repository it did not
# come from.
LABEL org.opencontainers.image.title="retina-magnetometer" \
      org.opencontainers.image.description="RM3100 magnetometer app for RETINA nodes, with a register-level RM3100 simulator"

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv sync --locked --no-dev --no-cache --compile-bytecode

COPY retina_magnetometer ./retina_magnetometer
COPY rm3100_sim ./rm3100_sim

# Runs as root, deliberately, as retina-telemetry does. On a node the app
# opens /dev/i2c-1, which the host creates root:i2c 0660 with a group id that
# varies by image, and writes a bind mount that Docker creates root-owned on
# first start. Root inside the container is the owner of both without any
# capability, so the node compose entry drops them all (cap_drop: ALL), runs
# with a read-only root filesystem and no-new-privileges, and the only thing
# it serves is read-only.
#
# No HEALTHCHECK: health is a payload (status.json and /api/health), not a
# container state. An unhealthy container holds up the Mender install of the
# whole node, and "no sensor fitted" is something to report, not a failure.
EXPOSE 3030
VOLUME ["/data"]
ENTRYPOINT ["python", "-m"]
CMD ["retina_magnetometer"]
