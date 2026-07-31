# syntax=docker/dockerfile:1
# Both tags are paired with reviewed multi-platform manifest digests. Refresh
# the tag and digest together so base or installer changes remain visible.
FROM ghcr.io/astral-sh/uv:0.12.0@sha256:606e70c71c852d03f611b1e56a195d08648507018a7057fab82c4974c4eae105 AS uv
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS build

WORKDIR /app
ENV UV_LINK_MODE=copy

# Build context is the workspace root (see build_push.sh). Install the engine
# and the declared adapters from the checked-in lock. --locked rejects stale
# project metadata instead of resolving new versions during an image build.
COPY --from=uv /uv /uvx /bin/
COPY . .
ARG LIBRAE_VERSION
ARG LIBRAE_REVISION
ARG UV_DEFAULT_INDEX
ARG UV_INDEX
ARG UV_FIND_LINKS
RUN test -n "${LIBRAE_VERSION}" \
    && test -n "${LIBRAE_REVISION}" \
    && SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LIBRAE="${LIBRAE_VERSION}" \
       uv sync --locked --no-dev --no-editable --no-cache \
       --python /usr/local/bin/python --no-python-downloads \
       --extra calendars --extra cli --extra db --extra crypto-live \
       --extra telegram --extra tw-live --extra us-live

FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS runtime

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

COPY --from=build /app/.venv .venv/
COPY --from=build /app/uv.lock uv.lock

ARG LIBRAE_VERSION
ARG LIBRAE_REVISION
LABEL org.opencontainers.image.version="${LIBRAE_VERSION}" \
      org.opencontainers.image.revision="${LIBRAE_REVISION}"

# The caller selects this named context explicitly. Its contents become the
# stable `strategies` import package used by trade.sh.
COPY --from=strategy_source . strategies/

# No CMD: trade.sh passes the strategy command via docker run.
