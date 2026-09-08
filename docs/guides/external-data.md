# External market data and factors

Librae consumes prepared observations; it is not a general data-ingestion
platform.

| Mode | Data owner | Engine behavior |
|---|---|---|
| Backtest | Caller | Pass one prepared point-in-time DataFrame. `Backtest` does not read a DB or call a broker API. |
| Sim/live | Caller or built-in broker adapter | `LiveTrader` polls completed-bar snapshots. It does not subscribe to streaming ticks. |

For common backtest layouts, normalize explicitly before constructing the
engine:

```python
from librae import Backtest, MarketDataSubscription, normalize_bars

subscription = MarketDataSubscription(
    symbol="BTCUSDT",
    timeframe="H1",
    calendar_id="24/7",
    session_mode="extended",
    data_source="vendor-name",
    instrument_type="spot",
)

bars = normalize_bars(
    vendor_frame,
    column_mapping={
        "ticker": "symbol",
        "date": "datetime",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
    },
    subscription=subscription,
)
backtest = Backtest(
    data=bars,
    strategy=strategy,
    primary_subscriptions=(subscription,),
)
```

For a single-symbol DataFrame with a timezone-aware `DatetimeIndex`, pass
`symbol="BTCUSDT"` instead. The helper converts timestamps to UTC, sorts the
canonical index, validates OHLCV, and preserves extra feature columns. It does
not localize naive timestamps or infer vendor-specific fields.

## Broker OHLCV outside the engine

Broker adapters and credential types are public:

```python
from librae.brokers import CryptoAdapter, IBKRAdapter, ShioajiAdapter

adapter = CryptoAdapter(exchange_id="binance")
bars = adapter.fetch_ohlcv("BTC/USDT", "1h", limit=500)
```

Install the extra for the adapter you use — see the broker extras table in
[Optional infrastructure](optional-infrastructure.md#brokers).
`BinanceStocksAdapter` uses the separate `stocks-data` extra and currently
provides catalog/latest-quote access only; it does not fabricate historical
OHLCV.

For sim/live, `LiveTrader(adapter=...)` accepts either:

- a concrete adapter with `fetch_ohlcv`; Librae binds its configured venue
  symbol and routing fields; or
- a callable `(symbol, timeframe, limit, *, drop_incomplete=False) ->
  DataFrame`.

An injected source owns its actual market-data route; Librae does not assume
that `SymbolInfo.data_adapter` is still in use. A concrete source can declare
`market_data_route = "ibkr"` (or another native route name) when it wants that
adapter's argument binding and route-specific startup checks. A source used for
an instrument without `calendar_id` must instead declare an exact
`market_data_calendar_id` such as `"XNYS"`; this keeps the six-field
subscription identity and daily completion boundary explicit without forcing
the registry entry to own that calendar. A missing calendar in both places
fails closed. These optional capability shapes are exported as
`MarketDataRouteOwner` and `MarketDataCalendarProvider` from
`librae.integrations`.

The reference deployment factory resolves these capabilities in two phases.
Built-in native routes are known from configuration, so a missing daily
calendar fails before their adapter is constructed. A caller-registered
factory owns its product's actual route instead: Librae constructs that
product, reads its route and calendar capabilities once, and validates the
result before database registration or polling begins. The registered factory
key selects construction only; it does not override the concrete source's
declared route. The resulting subscription snapshot is then shared unchanged
by persistence, runtime normalization, and adapter binding.

The result must contain UTC-aware `ts` plus OHLCV. When a source exposes a
publication time, map it to `available_at`; it must not precede the actual bar
completion. Extra columns are preserved and passed to `feature_fn`, except
reserved `available_at`: the live engine retains it only on the audit and
persistence view so publication metadata cannot become a strategy input.

`feature_fn` must return a non-empty DataFrame with a timezone-aware,
strictly increasing, unique `DatetimeIndex`. It may retain or drop older
warm-up rows, but it must not add observations later than the current event,
and its final row must represent that event exactly. A violation prevents
strategy evaluation and leaves the data watermark uncommitted for retry.

Cross-asset features opt in explicitly with `batch_feature_fn`; do not pass a
legacy `feature_fn` at the same time. The callback receives an immutable
`FeatureBatch` with the event timestamp, causal `as_of` frontier, configured
primary subscriptions, the active cohort, and an exact-identity
`MarketDataView`. It returns one DataFrame for every active subscription:

```python
def prepare_cross_asset(batch):
    latest = {
        subscription: batch.market_data.history(subscription)["close"].iloc[-1]
        for subscription in batch.primary_subscriptions
    }
    output = {}
    for subscription in batch.active_primary_subscriptions:
        frame = batch.market_data.history(subscription)
        frame["relative_close"] = latest[subscription] / sum(latest.values())
        output[subscription] = frame
    return output
```

The engine calls this once per committed primary cohort, including distinct
events that share the same `as_of`. It validates the complete mapping before
publishing any feature-derived bar or signal. In both backtest and live/sim,
`MarketDataView.history()` exposes at most `ExecutionPolicy.warmup_periods`
rows per primary or auxiliary subscription; an explicit smaller `limit` is
honored, while a larger value cannot exceed that configured window. Live
auxiliary readiness and staleness policy are separate from this
primary-cohort contract.

## `timeframe` and `poll_seconds`

They intentionally remain separate:

- `timeframe` defines the strategy bar and completed-data event clock.
- `poll_seconds` defines the whole runtime-loop cadence. Each cycle checks
  active orders, runs reconciliation when its separate interval is due, emits
  a heartbeat, and checks the completed-bar endpoint.

Sim/live requires an explicit polling cadence. Polling faster than the
timeframe may reduce detection latency but consumes more API quota; polling
slower than one bar emits a warning because bars may be observed late.
Only a newly completed bar recalculates equity/drawdown and invokes the
strategy. The current OHLCV polling path does not fetch an independent
intrabar quote, so it must not be described as high-frequency mark-to-market
risk monitoring. Streaming subscriptions and tick-driven strategies are not
currently supported.

Configure the value at the top level of strategy YAML or on the CLI:

```yaml
poll_seconds: 60
reconciliation_interval_seconds: 300
```

```bash
python -m my_strategy.run --mode live --poll-seconds 60 \
    --runtime-revision strategy-package-sha256-or-clean-source-revision
```

Closed-market suppression is not built into the runtime today. Do not stop the
whole loop: active orders and broker reconciliation may still need attention.
If API quota matters, wrap the market-data callable with the instrument's
calendar/session policy and return its unchanged cached frame while closed.
That produces no new decision. A generic scheduler, a second quote cadence,
and quote-driven risk rules should wait for a concrete strategy requirement
because their session, extended-hours, and mark-price semantics are distinct.

## Third-party factors in sim/live

Keep external I/O and point-in-time alignment in a user-owned composite
fetcher, then inject that fetcher as `adapter`. Use the factor's
`available_at`, not its economic/reporting date, and backward/as-of join it to
bars. Do not make remote calls inside `feature_fn`: that creates inconsistent
snapshots and makes retry behavior ambiguous.

```python
import pandas as pd

from librae.live.engine import LiveTrader

from examples.custom_data_provider import (
    CompositeBarFetcher,
    require_factor_and_add_signals,
)

provider = CompositeBarFetcher(
    price_fetcher=my_broker_bar_fetcher,
    factor_fetcher=my_factor_fetcher,
    max_factor_age=pd.Timedelta("2h"),
)
trader = LiveTrader(
    strategy=my_strategy,
    feature_fn=require_factor_and_add_signals,
    config=config,
    adapter=provider,
)
```

The [runnable example](../../examples/custom_data_provider.py) shows the
complete as-of join. If a required factor is missing or stale, raise from
`feature_fn`. Librae records no new strategy decision and leaves the market
data watermark uncommitted so the event can be retried. A previously queued
simulated action may still execute on its already-promised next bar before
feature calculation; factor failure must not rewrite that execution contract.

For backtests, perform the same point-in-time join before constructing
`Backtest`. The optional `external_factors` table is a persistence primitive,
not an automatic third-party ingestion service.

## Database boundary

The engine's default sim/live warm-up fetches directly through its injected
adapter. If you want DB-first history with API gap filling, implement that
policy in a callable and pass it as `warmup_fetcher`. The fetcher's third
argument is the requested history span, not a promised result size: the engine
may retry it with a larger value when de-duplicated, completed, currently
available observations do not meet `ExecutionPolicy.warmup_periods`. The
callable must not return a still-forming bar as completed. Direct `LiveTrader`
construction does not attach TimescaleDB. The repository orchestration factory
does so when `database_enabled=True`; live always requires an explicitly
injected durable `state_store`.
