# Getting started

This guide covers installation, a first run, and contributor setup. Engine
semantics and API details live in [`architecture.md`](../architecture.md);
runnable strategy patterns live in [`examples/`](../examples/).

## Requirements

- Python 3.12 or newer
- Git for direct repository installation
- [`uv`](https://docs.astral.sh/uv/) for the repository development workflow

TimescaleDB, Grafana, and broker SDKs are optional. A local backtest with
`--no-db` needs none of them.

## Install as a dependency

Librae is not on PyPI yet. Install the current default branch directly from
GitHub:

```bash
pip install "librae @ git+https://github.com/awwesomeman/librae.git"
```

An unpinned Git dependency moves with the default branch. Pin a tag or commit
for reproducible research and deployments:

```bash
pip install "librae @ git+https://github.com/awwesomeman/librae.git@<tag-or-commit>"
```

The version is derived from Git by `setuptools_scm`; inspect the installed
revision with `pip show librae` or `librae.__version__`.

### Optional dependencies

Install only the integration you use:

| Extra | Purpose |
|---|---|
| `db` | TimescaleDB persistence and durable live state |
| `crypto-live` | CCXT crypto adapter |
| `tw-live` | Shioaji Taiwan stocks and futures adapter |
| `us-live` | IBKR US stocks and futures adapter |
| `viz` | Local trade-chart viewer |

For example:

```bash
pip install "librae[db,crypto-live] @ git+https://github.com/awwesomeman/librae.git@<tag-or-commit>"
```

## Run the examples

The repository examples use deterministic synthetic data:

```bash
git clone https://github.com/awwesomeman/librae.git
cd librae
uv sync --extra test --extra dev
uv run python -m examples.simple_sma.run --mode backtest --no-db
```

Continue with the [examples guide](../examples/) to compare a single-asset
strategy, a prepared target-weight schedule, and cross-sectional Top K.

## Environment variables

Librae components read environment variables but do not load `.env` files.
The calling process chooses how to load them, for example:

```bash
uv run --env-file .env python -m your_strategy.run --mode backtest
```

When working from a clone:

```bash
cp .env.example .env
cp .env.secrets.example .env.secrets
```

Keep real trading/signing secrets in `.env.secrets`; the deployment scripts do
not sync that file. Placeholder values are sufficient for the test suite
because external broker and database calls are mocked.

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
