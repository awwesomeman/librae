FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# Install dependencies (ccxt is already a base dep; tw-live pulls in shioaji
# for Taiwan futures live trading)
COPY librae/pyproject.toml .
RUN pip install --no-cache-dir ".[tw-live]"

# Copy source. Build context is the workspace root (see build_push.sh) —
# librae/'s contents land flat at /app (its own pyproject.toml, librae/,
# db/, brokers/, notifications/, orchestration/, app/ all become top-level
# importable at /app); strategies/ + the run.py glue that depends on it
# come from the sibling workspace root. Both filtered by .dockerignore.
COPY librae/ .
COPY strategies/ strategies/
COPY orchestration_helpers.py .

# No CMD — trade_start.sh passes the command via docker run
