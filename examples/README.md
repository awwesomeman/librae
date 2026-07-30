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
| [`minimum_variance/`](minimum_variance/) | Keep a diagonal risk model and optimizer inside the strategy | `PortfolioTargets` | backtest |
| [`multi_leg_spread/`](multi_leg_spread/) | Open and close an explicitly sized relative-value spread | `MultiLegOrder` | backtest |
| [`custom_data_provider.py`](custom_data_provider.py) | Point-in-time third-party factor enrichment | data-provider callable | sim/live adapter boundary |

Run the backtests from the repository root:

```bash
uv run python -m examples.simple_sma.run --mode backtest --no-db
uv run python -m examples.target_weights.run --mode backtest --no-db
uv run python -m examples.topk_selection.run --mode backtest --no-db
uv run python -m examples.minimum_variance.run --mode backtest --no-db
uv run python -m examples.multi_leg_spread.run --mode backtest --no-db
uv run python -m examples.custom_data_provider
```

Start with `simple_sma` to learn the basic contract. Use `target_weights` when
allocations are produced by another research process, and `topk_selection`
when ranking and portfolio selection happen inside the strategy.
`minimum_variance` shows that covariance assumptions, objectives, and
constraints remain strategy-owned. `multi_leg_spread` demonstrates explicit
leg sizing without claiming atomic execution.

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

The examples keep strategy logic, matching, risk, and runtime settings in
separate namespaces:

```yaml
strategy:
  execution:
    default_fill_price: open
    max_bar_volume_participation_rate: 0.05
    adv_lookback_sessions: 20
    max_adv_participation_rate: 0.01
    # Live-only local cancel-and-halt fallback; not broker IOC/FOK/GTD.
    live_order_timeout_seconds: 120
  risk:
    max_position_weight: 0.30
    max_drawdown_rate: 0.20
    max_gross_exposure: 1.00
    max_net_exposure: 1.00
    max_order_notional: 50000
    max_limit_price_deviation_rate: 0.10
  params:
    lookback: 20
# Operational runtime settings (top-level, not strategy params):
poll_seconds: 5
reconciliation_interval_seconds: 300
market_data_workers: 1
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
`minimum_variance` estimates trailing per-symbol variance through bar T and
solves the diagonal-covariance minimum-variance weights in strategy code. All
three decide on bar T and fill on T+1.

The portfolio and multi-leg demos stay backtest-only because their bundled inputs are
synthetic and date-specific, not because `LiveTrader` rejects portfolio
or multi-leg decisions. A backtest-only runner rejects `--mode sim` or
`--mode live` before strategy execution and directs the developer to add real
market-data, broker, and state wiring. In real-time use, `PortfolioTargets`
waits for a complete required basket; per-symbol `OrderIntent` decisions can
execute asynchronously.

`PortfolioTargets` uses the configured next-bar fill field in backtest/sim.
Live sizes from the latest completed close and submits market orders whose
fills come only from broker reports. A per-symbol `OrderIntent.limit_price`
has the same limit-order meaning in every mode.

The SMA example also exposes the shadow-simulation path:

```bash
uv run python -m examples.simple_sma.run \
  --mode sim \
  --poll-seconds 5 \
  --no-db
```

`mode=sim` uses simplified bar fills. Broker paper trading uses `mode=live`
against a paper endpoint and requires an explicit broker route plus durable
state. When `live_order_timeout_seconds` is set, its age starts at the durable
wall-clock placement attempt. Expiry refreshes the broker state, cancels only
the remaining quantity, records any additional partial fill, and halts for
operator review. `null` leaves lifetime to the broker.

## Related multi-leg decisions

Use `MultiLegOrder` when explicitly sized legs belong to one decision and must
execute in a declared order. The
[`multi_leg_spread` example](multi_leg_spread/) is runnable and tests both
entry and exit groups:

```python
from librae import MultiLegOrder, OrderIntent

return MultiLegOrder(
    legs=(
        OrderIntent(action="long", symbol="TXF_NEAR", quantity=1),
        OrderIntent(action="short", symbol="TXF_NEXT", quantity=1),
    ),
    reason="calendar spread",
)
```

The contract also covers rolls, inventory hedges, and ordered exposure
transitions. Backtest/sim uses a synchronous OHLCV approximation. The generic
live runner rejects the group before submitting any leg; production execution
requires a venue-native combo adapter or a strategy-owned coordinator. The
exact boundary is defined in the
[multi-leg engine contract](../architecture.md#related-multi-leg-order-contract).

All legs in one runner use its single account:

```yaml
strategy:
  symbols: [NEAR_FUTURE, NEXT_FUTURE]
  account:
    account_id: futures
    currency: TWD
    initial_cash: 100000
```

Use separate `RunConfig` and runner instances for separate broker accounts or
currencies. A deployment layer may group their DB/UI output, but FX conversion,
settlement, funding transfers, and atomic cross-run fills remain outside the
engine.

For example, `("venue_a", 125.0, "USD")` and
`("venue_b", -80.0, "USD")` stay as two labeled results even though both
accounts use USD.

Before adapting any example to paper or live execution, complete the
[strategy readiness checklist](../docs/guides/strategy-readiness.md). It
covers point-in-time data, research validation, costs and capacity, risk,
reconciliation, broker certification, and intentional engine non-goals.

## Add infrastructure only when needed

`--no-db` skips analytics and state-store writes, so the backtests above need
no PostgreSQL or TimescaleDB. Database initialization, Grafana data flow, and
deployment examples are deliberately documented outside this tutorial:
[Optional infrastructure](../docs/guides/optional-infrastructure.md).

Return to the [project overview](../README.md), or continue with the
[architecture and complete API contract](../architecture.md).
