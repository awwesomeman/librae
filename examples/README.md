# Examples

The examples are runnable tutorials for the strategy-to-engine boundary. Each
uses deterministic synthetic data so you can inspect behavior without an API
key, database, or external data service.

> These examples demonstrate engine integration. They are not validated alpha,
> investment advice, or production-ready portfolio research.

## Pick an example

| Example | What it teaches | Strategy output | Supported demo modes |
|---|---|---|---|
| [`simple_sma/`](simple_sma/) | Single-asset entry and exit timing | `list[OrderIntent]` | backtest, shadow sim; live with explicit broker setup |
| [`target_weights/`](target_weights/) | Execute an externally prepared allocation schedule | `PortfolioTargets` | backtest |
| [`topk_selection/`](topk_selection/) | Rank a cross-sectional universe and select Top K | `PortfolioTargets` | backtest |

Run the backtests from the repository root:

```bash
uv run python -m examples.simple_sma.run --mode backtest --no-db
uv run python -m examples.target_weights.run --mode backtest --no-db
uv run python -m examples.topk_selection.run --mode backtest --no-db
```

Start with `simple_sma` to learn the basic contract. Use `target_weights` when
allocations are produced by another research process, and `topk_selection`
when ranking and portfolio selection happen inside the strategy.

## Shared layout

```text
example_name/
├── config.yaml    run-level inputs
├── strategy.py    feature preparation and strategy decisions
└── run.py         data retrieval, RunConfig, and engine wiring
```

The `run.py` module is intentionally part of each example. Librae provides
orchestration helpers, but your strategy project owns its data source,
configuration, output destination, and process lifecycle.

## Data contract

`Backtest(data=...)` expects a `DataFrame` with:

- an exact `MultiIndex` named `(symbol, datetime)`;
- numeric `open`, `high`, `low`, `close`, and `volume` columns; and
- feature columns computed only from information available at that timestamp.

The SMA example builds the index directly. Portfolio examples concatenate one
frame per symbol:

```python
df.index = pd.MultiIndex.from_arrays(
    [[symbol] * len(df), df.index],
    names=["symbol", "datetime"],
)
```

Live mode receives a plain per-symbol OHLCV frame and calls the strategy's
`prepare_signals(df)`. Reusing the same feature function across backtest and
live paths helps prevent research/production skew.

For exact validation, T → T+1 execution, incomplete baskets, and fill rules,
read the [engine usage contract](../architecture.md#usage).

## User-controlled trading settings

The examples keep strategy logic, matching, risk, and reporting in separate
namespaces. A complete configuration may look like:

```yaml
execution:
  default_fill_price: open
  max_bar_volume_participation_rate: 0.05
  adv_lookback_sessions: 20
  max_adv_participation_rate: 0.01
risk:
  max_position_weight: 0.30
  max_drawdown_rate: 0.20
  max_gross_exposure: 1.00
  max_net_exposure: 1.00
  max_order_notional: 50000
  max_limit_price_deviation_rate: 0.10
perf:
  periods_per_year: 8760  # H1 24/7 returns
params:
  lookback: 20
```

Each example config comments the values it chooses. The
[execution and risk reference](../architecture.md#execution-policy-risk-controls-and-portfolio-diagnostics)
is the SSOT for defaults, liquidity composition, calendars, decision fields,
protective-order timing, and failure behavior. Cost and instrument override
priority is documented under the
[Config API](../architecture.md#config-api).

## Portfolio example behavior

`target_weights` rotates through a precomputed schedule. `topk_selection`
computes trailing-return scores, ranks symbols at the same timestamp, selects
the highest-ranked names, and omits dropped names so the engine closes them.
Both decide on bar T and fill on T+1.

The portfolio demos stay backtest-only because their bundled inputs are
synthetic and date-specific, not because `LiveTrader` rejects portfolio
intents. In real-time use, `PortfolioTargets` waits for a complete required
basket; per-symbol `OrderIntent` decisions can execute asynchronously.

The SMA example also exposes the shadow-simulation path:

```bash
uv run python -m examples.simple_sma.run \
  --mode sim \
  --poll-seconds 5 \
  --no-db
```

`mode=sim` uses simplified bar fills. Broker paper trading uses `mode=live`
against a paper endpoint and requires an explicit broker route plus durable
state.

## Add infrastructure only when needed

`--no-db` skips analytics and state-store writes, so the backtests above need
no PostgreSQL or TimescaleDB. Database initialization, Grafana data flow, and
deployment examples are deliberately documented outside this tutorial:
[Optional infrastructure](../docs/guides/optional-infrastructure.md).

Return to the [project overview](../README.md), or continue with the
[architecture and complete API contract](../architecture.md).
