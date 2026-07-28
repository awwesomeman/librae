FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# Build context is the workspace root (see build_push.sh). Install the engine
# and all adapters used by the shared sim/live image.
COPY librae/ .
RUN pip install --no-cache-dir ".[db,crypto-live,tw-live,us-live]"

# Copy source. Build context is the workspace root (see build_push.sh) —
# Strategies remain a separate sibling repository.
COPY strategies/ strategies/

# No CMD: trade.sh passes the strategy command via docker run.
