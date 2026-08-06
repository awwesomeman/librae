# Librae

Librae is a Python engine for multi-asset backtesting, shadow simulation, and
broker-confirmed live execution. It gives research and execution the same
strategy interface.

> **Project status: alpha.** The research engine is usable, but live-trading
> readiness still depends on the selected broker adapter, account setup,
> operational controls, and strategy validation.

## Why Librae

- **One decision API** — express symbol orders with `OrderIntent`, complete
  allocations with `PortfolioWeights`, or tie explicitly sized `OrderIntent`s
  into a best-effort related order group with a shared `group_id`.
- **One execution-policy source** — fill-field, current-bar participation,
  optional session-level lagged-ADV assumptions, and the local live-order
  timeout live in typed
  `RunConfig.execution`, not free-form strategy parameters.
- **Causal execution** — simulations own the execution delay; live positions
  change only from broker execution reports.
- **Portfolio-aware core** — positions, cash, costs, exposure, concentration,
  turnover, target drift, and risk limits share one state model.
- **Composable boundaries** — the engine runs in memory; persistence, broker,
  notification, and operational integrations remain optional.
- **Explicit boundaries** — no hidden optimizer, feature pipeline, exchange
  calendar-driven event generation, FX ledger, or silent data repair.

## Choose your path

| You are... | Start here | Go deeper |
|---|---|---|
| Strategy developer | Run and adapt a [single-asset or portfolio example](examples/README.md) with deterministic data | Review [performance analysis](docs/guides/performance-analysis.md), the [strategy readiness checklist](docs/guides/strategy-readiness.md), [data and execution contract](docs/guides/engine-usage.md), and [signal outcome analysis](docs/guides/signal-outcome-analysis.md) |
| Backend/platform developer | Read the [system architecture](architecture.md) | Review [optional infrastructure](docs/guides/optional-infrastructure.md), callbacks, adapters, and durable state |

## Quick start

Librae requires Python 3.12 or newer.

Install the library directly from GitHub:

```bash
python -m pip install "librae @ git+https://github.com/awwesomeman/librae.git"
```

The base install contains the in-memory backtest engine and depends only on
NumPy and pandas. Reports, exchange calendars, CLI wiring, persistence,
notifications, UI, and broker integrations are opt-in extras. Librae is not on
PyPI yet; pin a tag or commit to identify Librae source, and lock the calling
application's complete dependency set when the environment must be
repeatable. See [Getting started](docs/getting-started.md) for package-only,
editable, restricted-network, and checkout workflows.

To run a complete example from a clone, see
[Run the examples](docs/getting-started.md#run-the-examples).

A strategy implements `on_bar(ctx)` and returns `OrderIntent` objects
(optionally grouped via `group_id`) or `PortfolioWeights`. Your data pipeline supplies timezone-aware OHLCV and
point-in-time features; Librae owns validation, execution timing, portfolio
state, costs, and output. The [examples](examples/README.md) show the complete
`RunConfig`, DataFrame, and engine wiring.

## Engine contract

- **Data acquisition:** `Backtest` only consumes the DataFrame you pass; it
  never reads a DB or calls an API. Sim/live polls completed-bar snapshots
  through a broker adapter or injected callable; streaming data is not yet
  supported.
- **Runtime polling:** `poll_seconds` is user-controlled and runs market-data,
  active-order, heartbeat, and due-reconciliation checks. It does not create
  intrabar strategy decisions or quote-based PnL marks.
- **Backtest input:** a `DataFrame` indexed exactly by
  `(symbol, datetime)`, with valid OHLCV and any precomputed feature columns.
- **Decision timing:** the strategy observes completed data; the engine owns
  the simulated T → T+1 execution delay. Do not pre-shift a signal to imitate
  that delay.
- **Portfolio logic:** alpha, objective, covariance model, and optimizer belong
  to the strategy. Librae accepts the resulting order intents or target weights
  and owns their validation, sizing, sequencing, execution, and diagnostics.
- **Outputs:** `BacktestOutput` contains run metadata, currency-labeled trade
  and funding events, one equity/PnL/metrics result per account, and optional
  position/allocation snapshots. For caller-owned Parquet, SQLite, or other
  local storage, use the format-neutral
  [artifact tables](docs/guides/local-artifacts.md). Build benchmark,
  active-return, and cross-run comparisons with the
  [performance analysis guide](docs/guides/performance-analysis.md).

The exact validation, fill, liquidity, margin, reconciliation, and state
semantics are documented in the [engine architecture](architecture.md#engine-design-librae).
For custom broker fetches and third-party factors, see
[External market data and factors](docs/guides/external-data.md).

## Scope at a glance

| Workflow | Current boundary |
|---|---|
| Single-asset research | Supported with next-observed-bar simulated fills |
| Cross-sectional selection and allocation | Predeclared candidate universe with point-in-time eligibility; optimizer remains strategy-owned; runtime symbol/subscription changes are not managed |
| Shadow simulation (`mode=sim`) | Simplified bar-fill monitoring, not broker paper trading |
| Paper/live broker execution (`mode=live`) | Broker-confirmed, restartable lifecycle with periodic reconciliation, single-process lease, post-fill risk checks, latency diagnostics, and explicit `time_in_force` (day/gtc/ioc/fok) per adapter |
| Related multi-leg execution | Explicitly sized `OrderIntent`s sharing a `group_id`, synchronous backtest/sim approximation; live submits legs serially and cancels only the failing group on a leg failure |
| Execution account | One named account and currency per run; use separate runs for separate broker accounts or currencies |
| Cross-account coordination | Caller-owned; the engine does not model FX, transfers, settlement, netting, or atomic execution across runs |
| Corporate actions / settlement | Must be adjusted or modeled upstream; no internal ledger |
| Short borrow / funding | Timestamped perpetual funding is supported in backtest/sim; no engine borrow/locate ledger |

See the [full capability matrix](docs/guides/engine-usage.md#use-case-capability-matrix)
and complete the [strategy readiness checklist](docs/guides/strategy-readiness.md)
before selecting or promoting a workflow.

## Documentation

The [documentation index](docs/README.md) separates current guides and architecture
from historical decisions, plans, research, and operational learnings.

**Deploying the optional infrastructure to a host with a public IP?** `.env.example`'s defaults (passwords, port binding) are for local dev only — read `SECURITY.md` first.

## Development

See [Getting started](docs/getting-started.md#contributing-to-this-repository)
for environment setup and the test/lint commands.

## License

[MIT](LICENSE)
