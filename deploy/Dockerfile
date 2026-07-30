# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# Build context is the workspace root (see build_push.sh). Install the engine
# and all adapters used by the shared sim/live image. Git metadata is excluded,
# so the build caller injects the source identity used by setuptools_scm.
ARG LIBRAE_VERSION
ARG LIBRAE_REVISION
COPY . .
RUN test -n "${LIBRAE_VERSION}" \
    && test -n "${LIBRAE_REVISION}" \
    && SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LIBRAE="${LIBRAE_VERSION}" \
       pip install --no-cache-dir ".[calendars,cli,db,crypto-live,telegram,tw-live,us-live]"
LABEL org.opencontainers.image.version="${LIBRAE_VERSION}" \
      org.opencontainers.image.revision="${LIBRAE_REVISION}"

# The caller selects this named context explicitly. Its contents become the
# stable `strategies` import package used by trade.sh.
COPY --from=strategy_source . strategies/

# No CMD: trade.sh passes the strategy command via docker run.
