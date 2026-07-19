FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# Install dependencies (ccxt is already a base dep; tw-live pulls in shioaji
# for Taiwan futures live trading)
COPY pyproject.toml .
RUN pip install --no-cache-dir ".[tw-live]"

# Copy source (filtered by .dockerignore)
COPY . .

# No CMD — trade_start.sh passes the command via docker run
