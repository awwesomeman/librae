# Getting started

This guide covers installation, a first run, and contributor setup. Engine
semantics and API details live in [`architecture.md`](../architecture.md);
runnable strategy patterns live in the [examples index](../examples/README.md).

## Requirements

- Python 3.12 or newer
- Git for direct repository installation
- [`uv`](https://docs.astral.sh/uv/) for the repository development workflow

The base install depends only on NumPy and pandas. TimescaleDB, Grafana,
reporting packages, exchange calendars, CLI wiring, notification clients, and
broker SDKs are optional. A direct Python backtest needs none of them.

## Install as a dependency

Librae is not on PyPI yet. Install the current default branch directly from
GitHub:

```bash
pip install "librae @ git+https://github.com/awwesomeman/librae.git"
```

An unpinned Git dependency moves with the default branch. Pin a tag or full
commit SHA for reproducible research and deployments:

```bash
pip install "librae @ git+https://github.com/awwesomeman/librae.git@<tag-or-full-sha>"
```

The version is derived from Git by `setuptools_scm`; `pip show librae` and
`librae.__version__` identify the installed build. In a mutable clone,
`librae/_version.py` is generated during installation and does not update
merely because the working tree changes. After switching revisions, rerun
`uv sync` or reinstall the package. Use `git rev-parse HEAD` to inspect the
working tree revision.

### Optional dependencies

Install only the integration you use:

| Extra | Purpose |
|---|---|
| `analytics` | Matplotlib trade and signal reports |
| `calendars` | Exchange-session labeling and resampling |
| `cli` | Repository YAML/CLI orchestration helpers |
| `db` | TimescaleDB persistence and durable live state |
| `crypto-live` | CCXT crypto adapter |
| `tw-live` | Shioaji Taiwan stocks and futures adapter |
| `us-live` | IBKR US stocks and futures adapter |
| `stocks-data` | Binance Stocks catalog and latest quotes |
| `telegram` | Telegram notifications |
| `viz` | Local trade-chart viewer |

For example:

```bash
pip install "librae[db,crypto-live] @ git+https://github.com/awwesomeman/librae.git@<tag-or-full-sha>"
```

## Run the examples

The repository examples use deterministic synthetic data:

```bash
git clone https://github.com/awwesomeman/librae.git
cd librae
uv sync --extra test --extra dev
uv run python -m examples.simple_sma.run --mode backtest --no-db
```

Continue with the [examples guide](../examples/README.md) to compare a
single-asset strategy, a prepared target-weight schedule, cross-sectional
selection, a strategy-owned optimizer, and an explicitly sized multi-leg
decision. Use the [strategy readiness checklist](guides/strategy-readiness.md)
before promoting a strategy beyond research.

## Environment variables

Librae components read environment variables but do not load `.env` files.
The calling process chooses how to load them, for example:

```bash
uv run --env-file .env python -m your_strategy.run --mode backtest
```

When working from a clone:

```bash
cp .env.example .env
mkdir -p .credentials
cp .env.secrets.example .credentials/ibkr-main.env
chmod 600 .credentials/ibkr-main.env
```

Keep real trading/signing secrets in account-specific files under
`.credentials/`; the deployment scripts do not sync that directory.
`trade.sh` passes one explicitly selected file to Docker with `--env-file`.
Placeholder values are sufficient for the test suite because external broker
and database calls are mocked.

If you installed the package without cloning the repository, scaffold the
minimal template with:

```bash
librae init
cp .env.example .env
```

See [Optional infrastructure](guides/optional-infrastructure.md) before
enabling a database, monitoring, or broker integration.

## Contributing to this repository

Set up all development integrations you intend to test:

```bash
uv sync --extra test --extra dev --extra db --extra crypto-live
git config core.hooksPath .githooks
```

Add `--extra tw-live`, `--extra us-live`, or `--extra viz` only when needed.
Run commands through `uv run`:

```bash
uv run pytest tests/ -q
uv run ruff check .
uv run ruff format --check .
```

The pre-commit hook runs the Ruff checks. Tests that exercise external
boundaries use mocks unless explicitly documented otherwise.
