# Librae

Librae is a Python engine for multi-asset backtesting, shadow simulation, and
broker-confirmed live execution. It gives research and execution the same
strategy interface while keeping market data, portfolio construction, broker
routing, persistence, and monitoring replaceable.

> **Project status: alpha.** The research engine is usable, but live-trading
> readiness still depends on the selected broker adapter, account setup,
> operational controls, and strategy validation.

## Why Librae

- **One decision API** — express single-symbol orders with `Action` or complete
  portfolio allocations with `RebalanceTargets`.
- **Causal execution** — backtest and shadow-sim decisions made on completed bar
  T become eligible on the next observed bar; live positions change only from
  broker execution reports.
- **Portfolio-aware core** — positions, cash, costs, exposure, concentration,
  turnover, target drift, and risk limits share one state model.
- **No required infrastructure** — the engine runs on an in-memory DataFrame.
  TimescaleDB, broker adapters, Telegram, Grafana, and deployment scripts are
  optional reference implementations.
- **Explicit boundaries** — no hidden optimizer, feature pipeline, exchange
  calendar, FX ledger, or silent data repair.

## Choose your path

| You are... | Start here | Go deeper |
|---|---|---|
| Quant analyst | Run the [SMA or portfolio examples](examples/) with deterministic data | Review the [data and execution contract](architecture.md#usage) and [signal outcome analysis](docs/guides/signal-outcome-analysis.md) |
| Strategy developer | Read the strategy shape below, then adapt an [example strategy](examples/) | Use the [core types and Config API](architecture.md#core-types) |
| Backend/platform developer | Read the [system architecture](architecture.md) | Review [optional infrastructure](docs/guides/optional-infrastructure.md), callbacks, adapters, and durable state |

## Quick start

Librae requires Python 3.12 or newer.

Install the library directly from GitHub:

```bash
pip install "librae @ git+https://github.com/awwesomeman/librae.git"
```

It is not on PyPI yet. For a reproducible environment, pin the dependency to a
tag or commit. See [Getting started](docs/getting-started.md) for extras,
versioning, environment variables, and contributor setup.

To run a complete example from a clone:

```bash
uv sync --extra test --extra dev
uv run python -m examples.simple_sma.run --mode backtest --no-db
```

The minimal strategy contract looks like this:

```python
from librae import Action, BaseStrategy, Context


class MyStrategy(BaseStrategy):
    def on_bar(self, ctx: Context) -> list[Action]:
        if ctx.positions.get(ctx.symbol):
            return (
                [Action(type="close", symbol=ctx.symbol)]
                if ctx.bar.get("exit_signal")
                else []
            )
        return (
            [Action(type="long", symbol=ctx.symbol)]
            if ctx.bar.get("entry_signal")
            else []
        )
```

Your data pipeline supplies timezone-aware OHLCV and point-in-time features;
Librae owns validation, execution timing, portfolio state, costs, and output.
Use the runnable examples for complete `RunConfig`, DataFrame, and engine
wiring rather than copying an incomplete snippet.

## Mental model

```text
market data + point-in-time features
                ↓
       strategy.on_bar(ctx)
                ↓
  Action(s) or RebalanceTargets
                ↓
 backtest / shadow sim / live engine
                ↓
 trades + equity + risk + diagnostics
```

- **Backtest input:** a `DataFrame` indexed exactly by
  `(symbol, datetime)`, with valid OHLCV and any precomputed feature columns.
- **Decision timing:** the strategy observes completed data; the engine owns
  the simulated T → T+1 execution delay. Do not pre-shift a signal to imitate
  that delay.
- **Portfolio logic:** optimizer and alpha logic belong to the strategy.
  Librae accepts the resulting actions or target weights and handles execution.
- **Outputs:** `BacktestOutput` contains run metadata, events, equity,
  performance metrics, and optional position/allocation snapshots.

The exact validation, fill, liquidity, margin, reconciliation, and state
semantics are documented in the [engine architecture](architecture.md#backtest-engine-design-librae).

## Scope at a glance

| Workflow | Current boundary |
|---|---|
| Single-asset research | Supported with next-observed-bar simulated fills |
| Cross-sectional selection and allocation | Supported for a static configured universe; optimizer remains strategy-owned |
| Shadow simulation (`mode=sim`) | Simplified bar-fill monitoring, not broker paper trading |
| Paper/live broker execution (`mode=live`) | Broker-confirmed order lifecycle; sequential, not atomic across a basket |
| Arbitrage | OHLCV research approximation only; no atomic multi-leg production execution |
| Multi-currency portfolios | Not yet modeled; the live/sim cash ledger is single-currency |

This table is a navigation aid, not a production-readiness claim. See the
[full capability matrix](architecture.md#use-case-capability-matrix) before
selecting a workflow.

## Optional integrations

The core package does not require these components:

| Directory | Role |
|---|---|
| [`brokers/`](brokers/) | CCXT crypto, Shioaji Taiwan, and IBKR US/futures adapters |
| [`db/`](db/) | TimescaleDB analytics and durable live runtime state |
| [`notifications/`](notifications/) | Telegram notifications |
| [`orchestration/`](orchestration/) | CLI/config helpers used by a strategy's `run.py` |
| [`app/`](app/) and [`deploy/`](deploy/) | Grafana and Docker/VM reference operations |

Use, replace, or omit them through the engine's injection points. Setup and
data-flow details live in [Optional infrastructure](docs/guides/optional-infrastructure.md).

## Documentation

| Document | Use it for |
|---|---|
| [Getting started](docs/getting-started.md) | Installation, extras, local setup, and first run |
| [Examples](examples/) | Runnable single-asset and portfolio strategy patterns |
| [Architecture](architecture.md) | Current system design, execution semantics, Config API, database conventions, and naming |
| [Signal outcome analysis](docs/guides/signal-outcome-analysis.md) | Forward return, MFE, and MAE research |
| [Optional infrastructure](docs/guides/optional-infrastructure.md) | TimescaleDB, Grafana, broker, notification, and deployment references |
| [Documentation index](docs/) | Decisions, plans, research, spikes, and learnings |

## Development

```bash
uv run pytest tests/ -q
uv run ruff check .
uv run ruff format --check .
```

See [Getting started](docs/getting-started.md#contributing-to-this-repository)
for the full environment setup.

## License

[MIT](LICENSE)
