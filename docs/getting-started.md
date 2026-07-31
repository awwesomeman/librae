# Getting started

This guide covers installation, a first run, and contributor setup. Engine
semantics and API details live in [`architecture.md`](../architecture.md);
runnable strategy patterns live in the [examples index](../examples/README.md).

## Requirements

- Python 3.12 or newer
- Git only for a repository clone or direct Git installation
- [`uv`](https://docs.astral.sh/uv/) for the repository development workflow

The base install depends only on NumPy and pandas. TimescaleDB, Grafana,
reporting packages, exchange calendars, CLI wiring, notification clients, and
broker SDKs are optional. A direct Python backtest needs none of them.

## Choose an installation workflow

| Workflow | Network at install time | Provides | Does not provide |
|---|---|---|---|
| Clone + editable install | Git host and package index or configured caches | Installed package plus repository examples, tests, Compose files, and deployment scripts | A frozen application dependency graph unless the caller keeps one |
| Direct Git dependency | Git host and package index or configured caches | Installed package and packaged SQL/Grafana resources | Repository examples, Compose files, deployment scripts, or offline installation |
| Built wheel | Only the wheel's runtime dependencies | Installed package and packaged resources without a checkout | Repository-only files |
| Target-specific wheelhouse | No network on the target | Installed package, selected extras, and their runtime dependencies | One wheelhouse that is automatically portable across every OS, architecture, and Python version |

The wheel intentionally contains `librae.*`, the console entry point, SQL
schema files, and Grafana provisioning data. It does not contain `examples/`,
`deploy/`, `tests/`, or the documentation tree. Clone the repository when you
want the runnable examples or reference Docker/VM workflow. A package-only
application can use the installed integrations with its own infrastructure.

## Install as a dependency

Librae is not on PyPI yet. Install the current default branch directly from
GitHub:

```bash
python -m pip install "librae @ git+https://github.com/awwesomeman/librae.git"
```

An unpinned Git dependency moves with the default branch. Pin a tag or full
commit SHA to identify the Librae source:

```bash
python -m pip install "librae @ git+https://github.com/awwesomeman/librae.git@<tag-or-full-sha>"
```

This does not pin NumPy, pandas, or optional integration dependencies. Keep a
lock file or wheelhouse in the calling application when the complete
environment must be repeatable.

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
python -m pip install "librae[db,crypto-live] @ git+https://github.com/awwesomeman/librae.git@<tag-or-full-sha>"
```

### Restricted-network installation

On a networked machine matching the target OS, architecture, and Python
version, build Librae and the runtime dependencies for the selected extras
into a normal wheelhouse:

```bash
python -m pip wheel --wheel-dir wheelhouse "librae[db] @ git+https://github.com/awwesomeman/librae.git@<full-sha>"
```

Copy `wheelhouse/` to the target, then install without contacting an index:

```bash
python -m pip install --no-index --find-links wheelhouse "librae[db]"
```

Because the target installs a built Librae wheel, it does not need Git or
PEP 517 build tools. Build a separate wheelhouse when the target platform,
Python version, or selected extras differ. Configure private indexes,
certificates, proxies, authentication, and caches through normal `pip` or
organization policy; do not disable TLS verification in project commands.

## Run a package-only backtest

This example runs from an installed distribution. It does not import the
repository `examples` package or enable a database, UI, notifier, or broker:

```python
import pandas as pd

from librae import Backtest, Context, OrderIntent, Strategy


class BuyOnce(Strategy):
    def on_bar(self, context: Context) -> list[OrderIntent]:
        if context.period_index == 0:
            return [
                OrderIntent(action="long", symbol=context.symbol, quantity=1.0)
            ]
        return []


timestamps = pd.date_range("2025-01-01", periods=6, freq="h", tz="UTC")
index = pd.MultiIndex.from_arrays(
    [["BTCUSDT"] * len(timestamps), timestamps],
    names=["symbol", "datetime"],
)
close = pd.Series(
    [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
    index=index,
)
data = pd.DataFrame(
    {
        "open": close,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": 1_000.0,
    },
    index=index,
)

backtest = Backtest(
    data=data,
    strategy=BuyOnce(),
    initial_balance=10_000.0,
    data_source="synthetic",
)
backtest.run()
print(backtest.build_output().metrics)
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

POSIX shell:

```bash
cp .env.example .env
mkdir -p .credentials
cp .env.secrets.example .credentials/ibkr-main.env
chmod 600 .credentials/ibkr-main.env
```

PowerShell:

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force .credentials
Copy-Item .env.secrets.example .credentials/ibkr-main.env
```

On Windows, apply the account-file ACL required by your organization and
runtime instead of POSIX `chmod`.

Keep real trading/signing secrets in account-specific files under
`.credentials/`; the deployment scripts do not sync that directory.
`trade.sh` passes one explicitly selected file to Docker with `--env-file`.
Placeholder values are sufficient for the test suite because external broker
and database calls are mocked.

If you installed the package without cloning the repository, scaffold the
minimal template with:

POSIX shell:

```bash
librae init
cp .env.example .env
```

PowerShell:

```powershell
librae init
Copy-Item .env.example .env
```

`librae init` writes only `.env.example`. It does not create a strategy,
Compose files, credentials, or deployment scripts.

See [Optional infrastructure](guides/optional-infrastructure.md) before
enabling a database, monitoring, or broker integration.

## Contributing to this repository

Set up all development integrations you intend to test:

```bash
uv sync --extra test --extra dev --extra db --extra crypto-live
git config core.hooksPath .githooks
```

Standard editable installation is also supported when the caller does not use
the repository's `uv` environment:

```bash
python -m pip install --editable .
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
