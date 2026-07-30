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
from librae import Backtest, normalize_bars

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
)
backtest = Backtest(data=bars, strategy=strategy, config=config)
```

For a single-symbol DataFrame with a timezone-aware `DatetimeIndex`, pass
`symbol="BTCUSDT"` instead. The helper converts timestamps to UTC, sorts the
canonical index, validates OHLCV, and preserves extra feature columns. It does
not localize naive timestamps or infer vendor-specific fields.

## Broker OHLCV outside the engine

Broker adapters and credential types are public:

```python
from brokers import CryptoAdapter, IBKRAdapter, ShioajiAdapter

adapter = CryptoAdapter(exchange_id="binance")
bars = adapter.fetch_ohlcv("BTC/USDT", "1h", limit=500)
```

Install the extra for the adapter you use: `crypto-live`, `tw-live`, or
`us-live`. `BinanceStocksAdapter` uses the separate `stocks-data` extra and
currently provides catalog/latest-quote access only; it does not fabricate
historical OHLCV.

For sim/live, `LiveTrader(adapter=...)` accepts either:

- a concrete adapter with `fetch_ohlcv`; Librae binds its configured venue
  symbol and routing fields; or
- a callable `(symbol, timeframe, limit, *, drop_incomplete=False) ->
  DataFrame`.

The result must contain UTC-aware `ts` plus OHLCV. Extra columns are preserved
and passed to `feature_fn`.

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
python -m my_strategy.run --mode live --poll-seconds 60
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
policy in a callable and pass it as `warmup_fetcher`. Direct `LiveTrader`
construction does not attach TimescaleDB. The repository orchestration factory
does so when `database_enabled=True`; live always requires an explicitly
injected durable `state_store`.
