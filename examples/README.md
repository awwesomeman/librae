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

Run-wide execution assumptions live only under `strategy.execution`; they are
not strategy parameters or risk limits:

| Setting | Default from `build_config()` | Expected behavior |
|---|---:|---|
| `default_fill_price` | `open` | Backtest/sim uses this field on the next eligible bar when the decision has no `fill_price`. It does not invent a live fill; broker reports remain authoritative. |
| `max_volume_participation_rate` | `0.10` | One symbol can consume at most 10% of that bar's volume across all fills. Low volume causes a partial fill, zero/missing volume rejects it, and the remainder of a target rebalance is reconsidered only when the strategy emits another target. Set `null` only to model unlimited liquidity. |
| `adv_lookback_sessions` | `null` | Optional D1-only lookback. ADV is the mean volume of exactly N completed daily bars before the execution bar; the current bar is excluded. Configure together with `max_adv_participation_rate`. |
| `max_adv_participation_rate` | `null` | Optional cumulative limit as a fraction of lagged ADV. Before the full lookback exists, fills are rejected instead of assuming liquidity. Configure together with `adv_lookback_sessions`. |

Every example sets the fill field and current-bar cap explicitly. The D1
portfolio examples also enable the ADV pair; the H1 example omits it. Changing
modes therefore does not silently change their assumptions. Per-decision
controls stay on the decision itself:

For a daily strategy, the two liquidity budgets compose by taking the tighter
remaining quantity:

```yaml
execution:
  max_volume_participation_rate: 0.05
  adv_lookback_sessions: 20
  max_adv_participation_rate: 0.01
```

```text
max fill = min(5% of execution-bar volume, 1% of lagged ADV) - quantity already filled
```

ADV is deliberately rejected for intraday timeframes. Librae does not yet own
exchange session calendars or an intraday volume profile, so treating a UTC
date as every market's trading session would overstate or misassign liquidity.

| Decision field | Scope and behavior |
|---|---|
| `OrderIntent.quantity` | Requested units. `None` spends available cash for a single new position; multi-symbol strategies should size explicitly. |
| `OrderIntent.fill_price` | Backtest/sim: bar-field override or one-bar numeric limit. Live: `None` is market and a number is a broker limit; bar-field strings are rejected. |
| `OrderIntent.stop_price` / `take_profit_price` | Deterministic backtest/sim protection. Live requires broker-native protective orders. |
| `PortfolioTargets.weights` | Signed target portfolio weights; omitted held symbols target zero. |
| `PortfolioTargets.fill_price` | Optional backtest/sim bar-field override for the whole basket; unsupported in live. |

Portfolio controls live only under `strategy.risk`; every value is a ratio and
`null`/omission disables that limit:

| Setting | Expected behavior |
|---|---|
| `max_position_weight` | Caps a new position or addition as a fraction of latest known equity. |
| `max_gross_exposure` | Rejects a `PortfolioTargets` basket whose sum of absolute weights exceeds the limit. |
| `max_net_exposure` | Rejects a basket whose absolute signed-weight sum exceeds the limit. |
| `max_drawdown_rate` | Halts new entries and liquidates after the completed-bar drawdown breaches the limit. |

Commissions, tax, base tick slippage, `volume_impact_ticks`, multiplier, and
margin are instrument/cost assumptions under `cost_overrides` or
`symbol_overrides`. At 100% bar participation, `volume_impact_ticks` adds that
many slippage ticks; the addition scales linearly with participation. These
three namespaces are intentionally separate: `execution` controls matching,
`risk` controls portfolio limits, and `params` belongs only to strategy logic.

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
