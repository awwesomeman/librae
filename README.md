# Librae

Librae is a Python engine for multi-asset backtesting, shadow simulation, and
broker-confirmed live execution. It gives research and execution the same
strategy interface.

> **Project status: alpha.** The research engine is usable, but live-trading
> readiness still depends on the selected broker adapter, account setup,
> operational controls, and strategy validation.

## Why Librae

- **One decision API** — express single-symbol orders with `OrderIntent` or complete
  portfolio allocations with `PortfolioTargets`.
- **One execution-policy source** — fill-field, current-bar participation,
  and optional D1 lagged-ADV assumptions live in typed
  `RunConfig.execution`, not free-form strategy parameters.
- **Causal execution** — simulations own the execution delay; live positions
  change only from broker execution reports.
- **Portfolio-aware core** — positions, cash, costs, exposure, concentration,
  turnover, target drift, and risk limits share one state model.
- **Composable boundaries** — the engine runs in memory; persistence, broker,
  notification, and operational integrations remain optional.
- **Explicit boundaries** — no hidden optimizer, feature pipeline, exchange
  calendar, FX ledger, or silent data repair.

## Choose your path

| You are... | Start here | Go deeper |
|---|---|---|
| Strategy developer | Run and adapt a [single-asset or portfolio example](examples/README.md) with deterministic data | Review the [data and execution contract](architecture.md#usage), [core types and Config API](architecture.md#core-types), and [signal outcome analysis](docs/guides/signal-outcome-analysis.md) |
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

A strategy implements `on_bar(ctx)` and returns `OrderIntent` objects or
`PortfolioTargets`. Your data pipeline supplies timezone-aware OHLCV and
point-in-time features; Librae owns validation, execution timing, portfolio
state, costs, and output. The [examples](examples/README.md) show the complete
`RunConfig`, DataFrame, and engine wiring.

## Engine contract

- **Backtest input:** a `DataFrame` indexed exactly by
  `(symbol, datetime)`, with valid OHLCV and any precomputed feature columns.
- **Decision timing:** the strategy observes completed data; the engine owns
  the simulated T → T+1 execution delay. Do not pre-shift a signal to imitate
  that delay.
- **Portfolio logic:** alpha, objective, covariance model, and optimizer belong
  to the strategy. Librae accepts the resulting order intents or target weights
  and owns their validation, sizing, sequencing, execution, and diagnostics.
- **Outputs:** `BacktestOutput` contains run metadata, events, equity,
  performance metrics, and optional position/allocation snapshots.

The exact validation, fill, liquidity, margin, reconciliation, and state
semantics are documented in the [engine architecture](architecture.md#backtest-engine-design-librae).

## Scope at a glance

| Workflow | Current boundary |
|---|---|
| Single-asset research | Supported with next-observed-bar simulated fills |
| Cross-sectional selection and allocation | Configured candidate universe with point-in-time eligibility; optimizer remains strategy-owned |
| Shadow simulation (`mode=sim`) | Simplified bar-fill monitoring, not broker paper trading |
| Paper/live broker execution (`mode=live`) | Broker-confirmed order lifecycle; sequential, not atomic across a basket |
| Arbitrage | OHLCV research approximation only; production multi-leg execution requires an explicit venue/adapter capability |
| Multi-currency portfolios | Not yet modeled; the live/sim cash ledger is single-currency |
| Corporate actions / settlement | Must be adjusted or modeled upstream; no internal ledger |
| Dynamic universes | Membership/eligibility data remains upstream; runtime subscription lifecycle is not yet engine-managed |
| Short borrow / funding | User-supplied research costs only; no engine locate or borrow ledger |

See the [full capability matrix](architecture.md#use-case-capability-matrix)
before selecting a workflow.

## Documentation

The [documentation index](docs/README.md) separates current guides and architecture
from historical decisions, plans, research, and operational learnings.

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
