# Engine usage and runtime behavior

Concrete usage examples and runtime semantics for the backtest and live
engines: how to call `Backtest`/`LiveTrader`, and the exact behavior of
execution policy, risk controls, funding, reconciliation, staleness
detection, and monitoring. For system boundaries, layering, and the DB
schema, see `architecture.md`.

## Backtest

```python
from librae import Backtest, Strategy, OrderIntent, Context, RunConfig


# 1. Define a strategy
class MyStrategy(Strategy):
    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        if ctx.positions.get(ctx.symbol):
            if ctx.bar.get("exit_signal"):
                return [OrderIntent(action="close", symbol=ctx.symbol)]
            return []
        if ctx.bar.get("entry_signal"):
            return [OrderIntent(action="long", symbol=ctx.symbol)]
        return []


# 2. Run the engine (a RunConfig is usually built via librae.orchestration.cli.build_run())
df = fetch_and_prepare(symbol, months)  # your own ETL
bt = Backtest(data=df, strategy=MyStrategy(), config=config)
bt.run()

# 3. Get the result
output = bt.build_output()  # BacktestOutput
```

**Data format**: a MultiIndex DataFrame `(symbol, datetime)` + raw, unshifted
OHLCV + point-in-time feature columns. A strategy observes completed bar T and
the engine owns the execution delay, so an intent is first eligible on T+1.
Callers must not pre-shift prices or signals to model that delay; doing so
delays execution twice. Feature construction remains the caller's
responsibility and must not use information unavailable at T.

## Local artifact boundary

`RunOptions(database_enabled=False)` tells repository orchestration not to
attach TimescaleDB; it never selects another backend or writes a file
implicitly.
`build_market_data_artifact()` and
`build_backtest_artifact()` expose versioned metadata plus logical pandas
tables. Librae owns validation and table shape. The caller owns Parquet,
SQLite, DuckDB, or other serialization details, including paths, overwrite
policy, transactions, partitioning, and retention. There is intentionally no
storage registry or sink hierarchy. See the
[local artifact guide](docs/guides/local-artifacts.md).

Artifacts are for research/export and do not satisfy live mode's durable
state-store, active-order, reconciliation, or lease requirements.

**Unshifted is a timing contract, not a price-adjustment flag.** The engine
cannot infer whether OHLCV is adjusted. Execution-oriented tests should
normally supply historically observable, unadjusted prices. Librae currently
has no ledger model for splits, dividends/coupons, futures rolls, FX/base
currency conversion, cash yield, or settlement lag. Adjusted/continuous
series can be research inputs, but using them as fill prices makes the
cash-and-position simulation economically incomplete.

The backtest boundary fails fast on malformed input. Index levels must be
exactly `(symbol, datetime)`; `(symbol, datetime)` pairs are unique; timestamps
are timezone-aware and increasing within each symbol; and `config.symbols`, when
provided, exactly matches the symbols in the frame. Required OHLCV values are
numeric and finite, prices are positive, every bar satisfies
`low <= open/close <= high`, and volume is non-negative. Validation never
sorts, forward/backward-fills, clips, or otherwise repairs data because those
operations can change the point-in-time research sample and must remain
explicit ETL decisions. Runtime frames use the same OHLCV value validator before
entering the cache; an invalid refresh is logged and cannot create a new event.

For an `OrderIntent`, `limit_price` is a one-eligible-bar limit order. A
buy fills when the bar's low reaches the limit and a sell fills when its high
does; a gap through receives the opening price. An unreached limit expires
after that bar and is logged. Simulated market orders use
`ExecutionPolicy.default_fill_price`; a strategy never embeds a historical bar
field in its decision. `PortfolioTargets` always uses that run-wide simulated
market-fill policy; use per-symbol `OrderIntent`s for limits.

Historical data may additionally provide non-null boolean `can_buy` and
`can_sell` columns as a required pair. The data adapter must normalize
market-specific price limits, halts, auctions, and empty-book states into these
side-level facts. Entry, ordinary close, stop, liquidation, drawdown, and
terminal fills share the rule. Triggered adverse stops/liquidations remain
pending until the required side is tradable; a terminal backtest raises rather
than inventing liquidity. Omitting both columns explicitly means the data
source supplied no side-tradability state. These facts must never be inferred
from the selected execution broker because one broker may route many markets.
Shioaji's per-contract `limit_up`/`limit_down` fields are used only to validate
an outgoing limit price for that resolved venue contract; they are not a
cross-market backtest rule.

OHLCV cannot determine whether an intrabar high or low happened first. A new
position therefore receives same-bar stop/take-profit processing only when its
entry is known at the bar open (the execution policy selects `"open"` or a
limit gaps through at
open). Protection for a resting limit or another non-open field begins on the
next observed bar. This conservative rule prevents a target reached before the
entry from being recorded as profit without introducing an invented intrabar
path model.

## Account and multi-asset / stock-picking strategies

Each run owns exactly one named account, which is the cash and PnL SSOT for
every configured symbol. The account has one currency and one
cash/equity/risk ledger. `RunConfig.account` and `BacktestOutput.account` are
scalar values; `account_id` remains a stable persistence and reporting key.

Strategies normally use `ctx.cash` and `ctx.equity`; `ctx.account` and
`ctx.account_id` expose the same ledger when its currency or explicit id is
useful. A deployment with separate broker accounts or currencies runs one
Librae engine per account and coordinates them outside the engine. Librae does
not provide FX conversion, transfer, borrowing, settlement, cross-account
netting, or atomic execution across runs.

`librae.orchestration.supervisor` defines the optional boundary for that outer
coordination. A `DeploymentSpec` gives one process a stable `deployment_id`
bound to one `account_id`, currency, mode, strategy configuration, entry point,
and account-specific credential reference. The deployment identity survives
process restarts and is distinct from the engine's `run_id`.
`validate_deployments()` rejects duplicate deployment identities and rejects
two live deployments that claim the same account within one submitted
manifest. The engine independently holds a durable account lease before any
broker reconciliation or order work, so separate manifests and launch paths
cannot concurrently control the same declared `account_id`.

The `Supervisor` protocol exposes only `start`, `stop`, `inspect`, and
`restart`. Docker, systemd, Kubernetes, or another concrete process manager
implements those operations and remains the lifecycle source of truth.
`DeploymentStatus` carries observed identity, phase, timestamp, and optional
process, run, exit, and failure facts; it is not a second state store.
`librae.orchestration.docker_supervisor.DockerSupervisor` is the optional
reference adapter for `deploy/trade.sh`; it reconstructs status from Docker
rather than retaining a coordinator cache.

| Owner | Responsibility |
|---|---|
| Engine | One account ledger, strategy execution, orders, risk, and restart-safe trading state |
| External supervisor | Process lifecycle, restart/backoff policy, and resource isolation |
| Deployment adapter | Translation between the `Supervisor` protocol and one concrete process manager |
| DB, UI, monitoring, notifications | Read-only aggregation of account- and currency-labelled status |

An orchestration process that restarts must recover status by inspecting the
external supervisor and durable engine state. It must not infer status from an
in-memory coordinator cache. One failed or unknown deployment remains an
account-specific fact and cannot change the lifecycle of another deployment.
Readiness is published only after restored state, durable ownership, and
startup broker reconciliation have completed. Normal stop is bounded and
graceful; forced termination is an explicit operator action.

These identities have deliberately separate meanings:

| Identity | Meaning |
|---|---|
| `config_hash` | Resolved engine configuration; excludes orchestration and source revision |
| `runtime_revision` | Caller-owned code/image identity accepted by one live checkpoint |
| Image digest or image ID | Deployment artifact identity; the reference Docker flow passes the selected image ID as `runtime_revision` |
| `deployment_id` | Stable external process slot across restarts and revisions |
| `run_id` | Engine run identity restored from the accepted checkpoint |

The repository's deployment acceptance path remains outside the engine. It
publishes the real combined-source image through `build_push.sh` to a
disposable OCI registry, transfers the documented infrastructure subset
through `cloud_deploy.sh` to a clean disposable SSH host, and starts a
digest-pinned broker-free process with `trade.sh`. This certifies packaging,
transfer, Compose, schema, service-network, and process-lifecycle wiring. It
does not certify registry authentication, cloud security policy, broker
credentials, market data, or order execution.

Within an account the engine is portfolio-level. `on_bar()` can return
`OrderIntent`s for multiple symbols. `OrderIntent.quantity=None` spends the
account's available cash, so multi-symbol decisions should normally use
explicit quantities.

For allocation strategies, return one `PortfolioTargets` instead:

```python
from librae import PortfolioTargets

return PortfolioTargets(
    weights={"AAA": 0.50, "BBB": 0.45},
    reason="monthly allocation",
)
```

The strategy timestamps the target implicitly by returning it for `ctx.ts`
(bar T). The engine resolves it on T+1 using each symbol's actual fill price and
portfolio equity at those same execution prices. The run's `ExecutionPolicy`
selects the simulated fill field; allocation intent does not override execution
semantics.
Positive weights are long, negative weights are short, and a held symbol
omitted from `weights` targets zero. Reductions and closes execute first in
symbol order, then additions. If entry costs exceed available cash, all
addition quantities receive one common scale factor so symbol ordering does not
starve later assets.

`PortfolioTargets` uses the run's single account as its capital base. A
cross-account hedge or arbitrage strategy must coordinate explicitly sized
orders across separate runs; hedge ratios remain strategy-owned.

Weights need not sum to one; any remainder stays in cash. When
`Backtest(..., record_position_snapshots=True)` is enabled,
`BacktestOutput.position_snapshots` contains quantity, signed market value, and
signed realized weight (`market_value / equity`) for every open position on
every event. `BacktestOutput.allocation_snapshots` adds target weight, achieved
weight, and drift for every configured symbol. Both are opt-in because
retaining O(events × configured symbols) facts can be expensive for a large
universe.

## Static candidate universe and point-in-time selection

Backtest, sim, and live runs use a predeclared candidate universe. For stock
selection, configure a survivorship-bias-free candidate superset and provide
point-in-time membership or eligibility as input data. The strategy filters
the currently eligible observations before ranking or optimization and returns
a complete `PortfolioTargets`; an omitted holding is therefore targeted to
zero.

`ctx.symbols` is the configured universe. `ctx.available_symbols` and
`ctx.bars` contain only symbols with a real current bar, so a temporary missing
observation does not become a universe change. A last-known close is a
valuation mark only and cannot trigger execution, stops, signals, or
holding-age increments.

Runtime discovery of an undeclared instrument is not supported: Librae does
not add or remove symbols, create or cancel market-data subscriptions, or
warm up a newly discovered symbol while a process is running. Those lifecycle
operations require reconfiguration and restart; they are not behavior that
strategy code should emulate.

Per-symbol `OrderIntent`s become eligible on that symbol's next observed bar
and can wait indefinitely without blocking anything else. `PortfolioTargets`
is intentionally synchronous and must be immediately executable when
returned: the strategy checks `ctx.available_symbols` for every non-zero
target and currently held symbol *before* returning one — the engine does not
wait across periods for a grouped decision's data to arrive, and rejects one
that is missing a required bar rather than queueing it. Use per-symbol order
intents for asynchronous cross-market execution.

## Related multi-leg order contract

`MultiLegOrder` represents explicitly sized related orders for synchronous
research simulation. It covers spreads, rolls, inventory hedges, and ordered
cross-instrument exposure transitions without encoding strategy-specific
arbitrage types:

Atomic multi-leg execution means the venue accepts the group as one
all-or-none transaction: every leg fills or none does. Separate API requests,
whether serial or concurrent, do not provide that guarantee; compensating
orders can add further exposure instead of restoring a portfolio. Generic live
execution therefore rejects `MultiLegOrder` and halts before sending any leg.
If one venue or broker exposes a native combo order, use that adapter-specific
capability. Cross-venue coordination belongs to a strategy-owned deployment
coordinator with explicit partial-fill and recovery policy.

```python
from librae import MultiLegOrder, OrderIntent

return MultiLegOrder(
    legs=(
        OrderIntent(action="long", symbol="BTCUSDT", quantity=0.10),
        OrderIntent(action="short", symbol="BTCUSDT-PERP", quantity=0.10),
    ),
    reason="spot-perpetual basis",
)
```

Every leg requires an explicit symbol and quantity, symbols cannot repeat, and
tuple order is the simulation order. The strategy checks `ctx.available_symbols`
for every leg *before* returning a `MultiLegOrder` — one event must already
contain every leg, or the engine rejects the decision rather than waiting
across periods for it. Backtest/sim then executes a synchronous OHLCV
approximation. This is useful for strategy research but does not claim
intrabar sequencing, venue atomicity, or recoverability in production.

Examples include TAIFEX near/next-future/cash-proxy and Binance
spot/perpetual/delivery-future spreads. Every leg in one `MultiLegOrder` belongs
to the run's single account. Cross-account or cross-currency execution requires
separate runs and a strategy-owned coordinator with an explicit FX/valuation
model.

Continuous near/next/quarterly aliases are dynamic identities, not exact
contracts. They are orderable only when the selected adapter implements that
explicit route (for example Shioaji's native `TXFR1` or IBKR front-month
resolution); the repository's Crypto orchestration rejects its research-only
continuous series.
A strategy that needs expiry-stable exposure supplies `contract_month` and a
unique canonical `symbol`. Roll timing remains an upstream strategy decision;
the engine never silently converts an exact contract into a rolling alias.

Execution then deliberately diverges:

- **Backtest/sim:** an OrderIntent decided at T is eligible on that symbol's next
  observed raw bar. Bar fields, one-bar numeric limits, stops, take-profit,
  liquidation, and estimated costs belong to this deterministic simulation
  model.
- **Live:** each batch of newly completed bars is an event and its ready intent
  is submitted immediately. A delayed symbol may create a second event with
  the same timestamp. Current prices may size requests but local execution
  facts come only from broker reports.

OHLCV caches are sorted and deduplicated. Mode-specific backlog handling is
defined under data staleness below. Both modes advance a durable per-symbol
watermark only after successful processing. A late symbol at the same
timestamp is processed with any same-timestamp bars already present in cache,
so a waiting target basket can become executable. An older event that would
rewind the global clock fails explicitly. If a backtest still holds a symbol
without a real bar at the final
timestamp, forced liquidation fails explicitly rather than fabricating a fill
from its last mark.

This is a **data-driven event clock**, not a calendar-driven event generator.
Input timestamps must be timezone-aware and every adapter/ETL path must use
the same convention: `ts` is the UTC bar-start instant. A bar becomes eligible
only after its interval close; fixed-duration bars use `ts + timeframe`, while
calendar-aligned bars use the actual segment/session close. Adapters drop a
final still-forming interval. Keeping the vendor's bar start avoids shifting
shortened or session-ending bars to an invented duration.

`SymbolInfo.calendar_id` is the SSOT for mapping an observed timestamp to its
trading-session label. The built-ins use `24/7`, `XNYS`, and `XTAIFEX`;
unregistered instruments set `instrument_overrides[symbol]["calendar_id"]`.
Standard IDs are resolved by `exchange_calendars`. `XTAIFEX` (15:00 night
open) and `XTAIFEX_1725` (17:25 night open) are Librae extensions that label a
Taiwan-futures night session with its following regular-session date.
Shioaji's epoch correction remains adapter-specific and is not a calendar.

Calendars own session labeling and resampling boundaries only. The engine
still does not manufacture bars or infer suspensions, vendor outages,
settlement days, or missing observations. The current TAIFEX implementation
uses XTAI trading dates as its holiday-session source; users requiring a
product-specific exceptional closure must provide already filtered data.
Cross-market baskets must provide coherent event labels; broker submissions
remain sequential reductions-then-additions, not exchange-level atomic.
`PortfolioTargets` expresses a desired portfolio state, not a transactional
all-or-none order. Live target legs are replanned only after the previous
broker fill is confirmed, using actual cash and positions, so slippage and
confirmed fill outcomes change the remaining quantities instead of preserving a stale
precomputed basket.

Acknowledgement is not execution. Submitted/accepted/`cancel_pending`/cancelled/rejected
reports never mutate positions. A partial report commits only its confirmed
fill delta. Submitted, accepted, and partial orders remain in a durable,
serial queue and are polled before another strategy decision. Repeated
cumulative reports are idempotent; filled quantity, notional, commission,
slippage, and tax can only advance. Cancelled/rejected orders halt dependent
work. While an order remains active, polling still refreshes the OHLCV cache,
heartbeat, and staleness state, but it does not run a new strategy decision.
An optional local wall-clock timeout cancels an over-age order, preserves any
confirmed partial fill, and halts dependent work for review. A non-terminal
cancel acknowledgement remains `cancel_pending` and is reconciled without
issuing duplicate cancel requests. Operational/error
halts cancel tracked strategy orders. A drawdown
breach instead clears the pending strategy decision and keeps its emergency
reduce/close queue active until broker reports reach a terminal state,
including across restart. Local stop/take-profit parameters remain
simulation-only because they cannot be inferred later from a completed range.
Protective live orders require a broker-native implementation.

Every exposure-increasing live fill is checked again against confirmed
position, gross, and net limits. A breach halts dependent execution.

Incremental cache retention is capped by the validated
`ExecutionPolicy.warmup_periods` (default 720; an injected warmup
fetcher may provide more initial history). This implementation favors
daily/session correctness over high-frequency throughput; lower strategy
frequency reduces load but does not remove clock/order-state synchronization
requirements.

## Local trade-chart viewer

Use after `pip install -e ".[viz]"`. It renders the OHLCV and
`order_events` already present in `BacktestOutput`; it does not re-simulate
fills or recompute PnL. The plotted markers therefore reflect those event
records directly, while aggregate metrics remain owned by
`librae/core/metrics.py`.

```python
from librae import plot_kbars
from librae.db.charts import plot_trades_by_run_id

ohlcv = df.xs(symbol, level="symbol")  # a single symbol's OHLCV
plot_kbars(
    ohlcv, output.order_events, symbol
)  # right after a backtest run, output already in hand

plot_trades_by_run_id(
    run_id
)  # or: skip rerunning the backtest, read a persisted run straight from the DB
```

`librae.db.charts.plot_trades_by_run_id` reads persisted `trade_events` and `ohlcv`
rows through `librae.db.timescale_reader`. The database adapter then calls the same
format-neutral renderer as the in-memory form and does not rerun the strategy.

## Trade outcome analysis

`librae/core/metrics.py` is the SSOT for reconstructing analytics lifecycles
from canonical `open`/`add`/`reduce`/`close` events. A position lifecycle is
one per-symbol `0 → N → 0` interval; a realized exit is each `reduce` or
`close`. These are deliberately different populations. Incomplete
end-of-sample lifecycles are reported but excluded from completed-lifecycle
summaries.

Trade analytics require `Mapping[str, pd.DataFrame]`, with one sorted, unique,
timezone-aware OHLCV frame per event symbol. Horizons advance by subsequent
observed bars of that symbol, never by a portfolio-wide row shift. The public
fact/summary split is:

| Analysis | Facts | Summary weight |
|---|---|---|
| Actual lifecycle excursion | `compute_trade_lifecycle_outcomes` | equal completed lifecycles, pooled and per symbol |
| Hypothetical post-entry envelope | `compute_trade_entry_outcomes` | equal `open`/`add` anchors at each valid horizon, pooled and per symbol |

`split_lifecycle_by_oos_start(completed, entry_outcomes, oos_start)` splits
already-reconstructed lifecycle/entry-outcome tables by `closed_at`/`anchor_ts`
into in-sample/out-of-sample — split the computed tables, not `order_events`,
so a lifecycle straddling the cutoff is not misclassified as incomplete.
Charting or reporting on any of these DataFrames is caller-owned — librae only
ships `plot_kbars` (the K-line/marker overlay); see `examples/trade_report.py`
for the compute → chart pattern.

MFE/MAE are gross, direction-adjusted percentage-point price excursions;
costs and notional-weighted portfolio risk remain separate metrics. Adds
change the weighted-average basis prospectively, while reductions preserve
it. Full high/low ranges count only between events. On an event bar, analytics
use explicit fill-price state observations because OHLCV cannot establish
whether the bar extrema occurred before or after the fill. Exact intrabar
excursion therefore requires finer-grained data.

## Execution policy, risk controls, and portfolio diagnostics

Fill-field and liquidity assumptions have one typed source:
`RunConfig.execution`. `build_run()` defaults to next-open simulation and a
10% per-symbol bar-volume cap in every mode. Direct `Backtest(...)`
construction resolves the same `ExecutionPolicy()` defaults; unlimited
liquidity therefore requires
`execution=ExecutionPolicy(max_bar_volume_participation_rate=None)`.
When `config=` is supplied, passing a second `execution=` or `risk=` policy is
rejected instead of silently choosing one source.
Backtest/sim enforce configured liquidity caps in the fill model. Live uses
them for request sizing, then lets broker execution reports determine actual
fills. Emergency live exits bypass local caps so the broker owns partial-fill
truth.

```python
from librae import ExecutionPolicy, RiskPolicy, RunConfig

config = RunConfig(
    ...,
    reconciliation_interval_seconds=300,
    market_data_workers=1,
    execution=ExecutionPolicy(
        default_fill_price="open",
        max_bar_volume_participation_rate=0.1,
        # Optional session-level cap; both fields must be set together.
        adv_lookback_sessions=20,
        max_adv_participation_rate=0.01,
        warmup_periods=720,
        # Local fallback, not broker IOC/FOK/GTD.
        live_order_timeout_seconds=120,
    ),
    risk=RiskPolicy(
        max_position_weight=0.3,  # 30% of latest known equity
        max_gross_exposure=1.2,  # reject targets above 120% gross
        max_net_exposure=1.0,  # reject targets above 100% absolute net
        max_drawdown_rate=0.2,  # liquidate and halt after a 20% drawdown
        max_order_notional=25_000,  # reject larger entry/add orders
        max_limit_price_deviation_rate=0.1,  # live limit-price collar
    ),
    params={"lookback": 20},  # strategy logic only
)
```

`execution`, `risk`, and `params` are separate SSOTs. Putting execution or
risk keys in `params` raises immediately; the engine never reparses a
free-form strategy dictionary for portfolio controls.

`RunConfig.runtime.reconciliation_interval_seconds` and
`RunConfig.runtime.market_data_workers` are operational settings rather than
strategy semantics, so they do not change `config_hash`. Fetching is sequential
by default because many vendor SDK
clients are not thread-safe; a value above one opts into bounded per-cycle
concurrency. `LiveTrader.last_cycle_diagnostics` reports per-symbol fetch time,
strategy time, broker-order time, total cycle time, and whether the cycle
exceeded `RunConfig.runtime.poll_seconds`.

- `default_fill_price`: backtest/sim fallback for decisions without an
  explicit fill field. It is not used to manufacture live executions.
- `max_bar_volume_participation_rate`: one cumulative per-symbol volume budget per
  simulated data event across entries, additions, reductions, ordinary
  closes, stops, modeled liquidation, drawdown exits, and terminal exits.
  Missing volume rejects the fill. Constrained exits are explicit partial
  fills and retain the remaining position for a later observed bar. Once
  stop-market or liquidation has triggered, its remainder stays an active
  market exit. A terminal backtest that cannot finish exits raises.
- `adv_lookback_sessions` + `max_adv_participation_rate`: optional session
  capacity budget. ADV is the mean total volume of exactly N completed trading
  sessions before the active session; it never contains active-session volume.
  D1 treats each row as one session. Intraday aggregates observed bars using
  the symbol's `calendar_id`, which is required at startup. Missing warmup
  rejects fills. Bar-volume and ADV usage are tracked separately, so available
  quantity is `min(bar cap - filled this bar, ADV cap - filled this session)`.
  This avoids granting the full ADV budget again on every intraday bar. No
  intraday volume-profile estimate is needed: the current-bar cap remains the
  local liquidity constraint. Sim/live `ExecutionPolicy.warmup_periods` must retain enough
  bars to cover N full sessions. The pair is disabled by default.
- `warmup_periods`: positive live/sim feature-history retention count; it is
  typed engine configuration, not a strategy `params` fallback.
- `live_order_timeout_seconds`: optional live-only local safety timeout measured
  from the persisted wall-clock placement attempt. On expiry the engine first
  refreshes the broker report, requests cancellation only if the order remains
  non-terminal, applies any additional cumulative fill, then halts. `None`
  leaves lifetime to the broker. This is not broker-native time-in-force:
  IOC/FOK/GTD/DAY support remains adapter- and venue-specific.
- `max_position_weight`: both new entries and adds get capped (fills are recomputed with commission/slippage/tax after capping) — this isn't an outright rejection.
- `max_order_notional`: hard-rejects an individual exposure-increasing open or
  add above this account-currency notional after normal position/liquidity
  sizing. Reductions, closes, and emergency exits remain available.
- `max_limit_price_deviation_rate`: in live mode, rejects a broker-normalized
  limit price whose absolute distance from the latest completed close exceeds
  this ratio. Market orders are unaffected because their execution price is
  not known before submission.
- `max_gross_exposure` / `max_net_exposure`: backtest/sim validate every
  complete strategy decision batch (`OrderIntent`, `PortfolioTargets`, or
  `MultiLegOrder`) against staged post-decision positions before mutation.
  Live validates each request in its actual submission order as well as the
  final portfolio, so a later hedge cannot conceal a transient breach. A
  transition that worsens an already-breached limit is rejected before
  submission, while a risk-reducing transition remains allowed. Live
  additionally checks confirmed fills because broker slippage and partial
  fills can differ from the staged request.
  `calculate_signed_position_notionals()` and
  `calculate_position_weights()` are the shared accounting SSOT used by risk
  validation, backtest snapshots, live post-fill checks, and live diagnostics.
- `max_drawdown_rate`: once detected from a completed bar, backtest/sim queues
  market exits for the run's open positions and fills them at the next observed
  bar open (subject to the normal volume cap); it never observes a close and
  fills at that same close. Live submits immediate market closes and books only
  confirmed broker fills. The halt persists across restart, emergency exits
  remain active while halted, and broker orders must reach a terminal state
  before `reset_halt()` is allowed. After operator review, `reset_halt()` starts
  a new risk epoch.
- `LiveTrader.halt(reason)` is the operator kill switch: it persists the halt,
  clears pending strategy decisions, and cancels tracked live broker orders.
  `reset_halt()` is required after review before new entries resume.
- Volume-aware slippage (`CostModel.volume_impact_ticks`) is independent of this switch and also defaults to off: as long as volume data is supplied and that market/symbol's `volume_impact_ticks > 0` (set via `market_config.py`/`symbols.py`/`cost_overrides`), slippage scales linearly with the fill's share of that bar's volume, regardless of whether a cap is configured.

The backtest timeframe is inferred independently for each symbol. Every symbol
with enough observations must agree, while sparse histories must remain
aligned to that interval. The union event clock is never used to infer a
faster, synthetic timeframe for staggered markets.

Every `EquityCurvePoint` contains gross exposure, net exposure, concentration,
and one-way turnover (`sum(abs(traded notional)) / ending equity`) for its
event. Gross exposure is the engine's gross-leverage measure. `StrategyMetrics`
aggregates total turnover, average/maximum gross exposure, maximum absolute net
exposure, maximum concentration, and a small full-sample period-return summary.
`AllocationSnapshotPoint` provides attribution-ready target/achieved facts;
return attribution by factor, sector, or decision remains strategy/research
code because the engine has no classification model.

Performance analysis accepts an already-aligned period-return DataFrame.
`summarize_performance()` produces selectable full-sample scalar metrics, and
`compute_performance_series()` produces selectable path metrics. Columns have
no engine-assigned role, so strategies and reference series use the same
contract. Alignment, resampling, annualization, grouping, active-period
selection, attribution, and independent-run aggregation remain caller or
optional-reporting policy. This avoids engine APIs that name a benchmark while
leaving its economically important policies implicit. Examples are in the
[performance analysis guide](docs/guides/performance-analysis.md).

`available_metrics()` returns the static metric names supported by those two
APIs, optionally filtered with `kind="summary"` or `kind="series"`. It performs
no calculation, network discovery, plugin scan, or import of an optional
reporting package. The `DEFAULT_*_METRICS` tuples remain selection defaults,
not the capability boundary, so defaults may later become a supported subset
without making other metrics invalid.

## Perpetual funding cash flows

Backtest and shadow-simulation bars may contain a `funding_rate` observation,
expressed as a decimal rate for that payment. `funding_mark_price` is optional;
the same event's raw `close` is used when it is absent. Missing rates mean no
payment: the engine never forward-fills or fetches them.
When rates come from `external_factors`, the data pipeline exact-joins its
`value` as `funding_rate` on symbol and payment timestamp; an as-of join or
forward fill would turn one payment into repeated charges.

Funding is applied after the event's confirmed simulated fills and stops, and
before its equity snapshot and strategy decision. Positive rates mean longs
pay shorts:

`cash_flow = -side_sign * quantity * mark_price * multiplier * funding_rate`

where `side_sign` is `+1` for long and `-1` for short. Only the confirmed open
quantity at that timestamp participates. Payments update account cash, equity,
returns, and drawdown without changing execution prices or trade PnL.
`FundingCashFlowRecord` and `funding_cash_flows` retain the rate, mark,
quantity, multiplier, side, and realized cash flow for audit.

Paper and live broker modes do not apply these research observations. Broker
balances and exchange funding records remain authoritative. This contract
does not add rate fetching, FX conversion, settlement, transfer, or cross-run
netting.

## Margin / liquidation simulation

`CostModel.maintenance_margin_rate` (default 0 = off, following the same "belongs to the market/instrument, not `config.params`" convention as `volume_impact_ticks`, configured via `market_config.py`/`symbols.py`/`cost_overrides`). In backtest/sim, `resolve_stop_exit` checks every bar whether a position has hit the modeled liquidation price; if so it force-closes with `REASON_LIQUIDATION`, using conservative gap-through logic. The liquidation check takes priority over stop-loss/take-profit. Live does not replay this completed-bar touch as a market order: venue margin/liquidation and broker-native protective orders are authoritative.

The formula is a simplified isolated-margin approximation: long
`entry*(1 + maintenance_margin_rate - margin_rate)`, short
`entry*(1 - maintenance_margin_rate + margin_rate)`. It does not solve a
venue-specific liquidation threshold from fees or accumulated funding;
funding instead affects cash, equity, and drawdown through the event contract
above. Spot (`margin_rate=1.0`) never triggers unless
`maintenance_margin_rate` is set.

`margin_rate`/`maintenance_margin_rate` are always a fraction of notional, never an absolute currency figure — there's no config field for e.g. "NT$636,000 initial margin" directly; a caller converts from the exchange's published absolute figure to a ratio before setting it (see `market_config.py`'s `tw_futures` entry for a worked example). Treated as static for the whole run — see `docs/plans/enhance_librae_real_trade.md`'s item B for why, and its known blind spots.

## Reconciliation (live only)

Startup reconciliation runs automatically when `LiveTrader.run()` starts.
When no order or grouped execution is active, the checks repeat every
`reconciliation_interval_seconds`. Reconciliation is a no-op in sim mode:

- **Orders**: restore the checkpoint first, recover placement-attempted orders
  by deterministic client id, poll tracked orders, then compare the broker's
  open orders on configured symbols. An untracked order is treated as an
  orphan: do not guess ownership or invent a fill; halt for operator review.
- **Positions**: `get_position(PositionRequest)` is called only for this strategy's
  configured symbols; it is not described as a complete account snapshot. A
  restored checkpoint keeps its entry time and accumulated costs, while broker
  side/quantity is a reconciliation assertion. A mismatch or unreadable
  configured-symbol snapshot halts. A first run with no checkpoint must be
  flat: broker exposure alone cannot reconstruct the engine's cash, accumulated
  entry costs, or risk epoch, so a non-flat first run halts and requires the
  matching checkpoint or an operator flatten. Crypto spot uses base-asset
  balance inventory, not the derivatives-only positions endpoint. Ordinary
  spot also rejects opening/adding a short, while a sell that reduces or closes
  owned inventory remains valid.
- **Cash** (`_reconcile_cash`, for adapters exposing `get_balance()`): warns
  only, never overwrites. A Telegram alert fires once discrepancy exceeds
  `LiveTrader.CASH_RECONCILE_TOLERANCE_PCT` (default 1%). Broker free/total
  semantics vary by account mode, so blindly overwriting can corrupt a valid
  ledger. A missing capability or unreadable balance is logged explicitly;
  best-effort means non-fatal, not silent.

During a run, `ExecutionReport` is the only source that changes the local
position ledger. `execution_runtime_state` atomically checkpoints the cycle
timestamp, per-symbol bar watermarks, pending intent, cash, positions, last
prices, equity peak, halt/risk counters, target-rebalance lifecycle, funding
watermarks, and active order queue. Runtime-state schema v16 persists scalar
account state, the caller-owned `runtime_revision`, and exact/rolling contract
identity nested in active `OrderRequest` values. Only the current v16
checkpoint is accepted; every older schema requires an explicit external
migration or removal.
`_STATE_SCHEMA_VERSION` is the single code-level version constant and must be
bumped whenever the checkpoint or any persisted nested dataclass changes
shape; it is not a business/domain version.
`broker_orders` keeps completed
and active order facts for audit/idempotency without growing the checkpoint.
Placement-attempted and its UTC wall-clock timestamp are saved before network
I/O, so an ambiguous placement outcome is looked up rather than blindly
retried, and local order age survives restart. Analytics callbacks remain
projections; they are not broker fill truth. The checkpoint key is
`mode:config_hash`; `runtime_revision` deliberately does not change that key.
An existing live checkpoint with a missing or different revision is rejected
without conversion, discard, or overwrite. Selecting its matching old
revision is therefore a valid rollback; adopting a new revision requires an
explicit checkpoint migration or a flat-account reset. This compatibility
check happens during state restoration, before broker reconciliation, order
lookup, or submission. Live mode first holds a PostgreSQL session advisory lock
for `live-account:<account_id>`, then one for the checkpoint key, for the
process lifetime. The account lease prevents a different strategy or
configuration from controlling the same declared account; the checkpoint
lease prevents concurrent mutation of the same restart state. A second owner
fails before broker reconciliation or order submission. Custom durable stores
provide the equivalent `acquire_lease` / `release_lease` contract for both
namespaced keys.

## Data staleness detection (live only)

Checked on every poll cycle, including while an order is active, not just at
startup. `_check_staleness`
compares the latest completed bar timestamp with the UTC clock. A frame is
stale after `(STALE_DATA_TOLERANCE_BARS + 1) * timeframe` (three intervals by
default); the extra interval accounts for the normal age of a completed bar.
The alert is edge-triggered and re-arms after recovery. Sim keeps processing
for deterministic shadow/replay workflows, while live fails closed for that
poll: the stale frame never reaches the strategy or broker.

Live also does not replay an outage backlog into the market. If a fetch returns
multiple bars newer than the durable watermark, only the latest completed bar
is evaluated and the skipped count is alerted. Sim continues to replay every
uncommitted bar. The optional `clock` constructor dependency exists to make
time-based integrations deterministic; production defaults to UTC now.

## Sim monitoring

```python
from librae.orchestration.live import build_live_trader

trader = build_live_trader(
    strategy=MyStrategy(),
    feature_fn=prepare_signals,  # the same ETL pipeline
    config=config,  # a RunConfig (usually built via librae.orchestration.cli.build_run())
    # options is the companion RunOptions returned by build_run().
    database_enabled=options.database_enabled,
    telegram_config=options.telegram_config,
)
trader.run()
```

The factory composes built-in adapters and, when enabled, TimescaleDB and
Telegram. Advanced callers construct `LiveTrader` directly and inject
`adapter`, `order_adapter`, analytics callbacks, `notifier`, and `state_store`
independently. The engine never selects these implementations from config.

## Mode comparison

| | Backtest | Shadow sim (`mode=sim`) | Paper broker (`mode=live`) | Live broker |
|---|---|---|---|---|
| Data source | historical OHLCV | polled OHLCV | broker/vendor OHLCV | broker/vendor OHLCV |
| Decision timing | completed T | completed T | completed T | completed T |
| Execution timing | simulated on eligible T+1 | simulated on eligible T+1 | submit after T decision | submit after T decision |
| Fill truth | raw T+1 bar + `CostModel` | raw T+1 bar + `CostModel` | paper `ExecutionReport` only | broker `ExecutionReport` only |
| Funding truth | supplied timestamped bar observations | supplied timestamped bar observations | broker/exchange balance and records | broker/exchange balance and records |
| Non-final order | one-bar intent expires | one-bar intent expires | durable cumulative order lifecycle | durable cumulative lifecycle; optional local timeout/cancel |
| Restart | new run | restore when state is enabled | restore and reconcile | restore and reconcile |

## Use-case capability matrix

Use this matrix to select an engine workflow, then apply the
[strategy readiness checklist](docs/guides/strategy-readiness.md) before
promoting a strategy between research, shadow simulation, broker paper, and
live capital.

| Use case | Backtest | Shadow sim | Paper/live execution |
|---|---|---|---|
| Single asset | Supported research | Simplified bar simulation | Broker-confirmed lifecycle; adapter/account readiness is external |
| Related multi-leg execution | Synchronous `MultiLegOrder` OHLCV approximation | Synchronous approximation | Generic runner halts before submission; use a venue-native combo or strategy-owned coordinator |
| Portfolio optimization | Strategy-owned optimizer; configured candidate universe with point-in-time eligibility | Simplified sequential basket | Confirmed-fill replanning; sequential and non-atomic |
| Asset allocation | Supported within one account/data-event boundary | Simplified | FX, income, corporate actions, and settlement remain unsupported ledger features |
| Cross-account arbitrage | Separate independent runs; no synchronized engine contract | Same | Strategy-owned external orchestration; no shared funding or atomicity |
| Dynamic stock universe | Point-in-time selection within a predeclared candidate superset | Same predeclared universe | No runtime symbol add/remove, subscription changes, or automatic warm-up |
| Short borrow/funding | Timestamped perpetual funding; borrow costs remain upstream | Same funding contract | Broker/account responsibility; no engine borrow/locate ledger |

Daily strategy frequency reduces throughput requirements but not timestamp,
data, order, and restart synchronization requirements. The observed-data event
clock remains the boundary: calendars label supplied bars but do not generate
events.

## Intentional defaults and resiliency fallbacks

The repository-wide fallback rule is: defaults may express an explicit
research convention or preserve transport availability, but may not invent a
financial/execution fact.

- Every fallback must be one of: a documented domain invariant, an explicit
  feature-off state, or a resiliency path that preserves already-confirmed
  state and emits a diagnostic. Missing/invalid configuration and missing
  accounting, risk, position, or execution facts raise an explicit error.
  Catch-and-default behavior at those boundaries is prohibited.
- Direct `Backtest(...)` construction without a config or cost model uses
  `CostModel.zero()` and next-open fills as a documented research default.
  Production-like research should pass an explicit cost model.
- A registered spot instrument may derive multiplier `1.0`; contract
  multipliers, broker routes, currencies, and unresolved execution prices are
  never guessed.
- The live OHLCV cache may serve the last successfully fetched history after a
  transient fetch error. It does not create a new bar or fill, and staleness
  monitoring remains active.
- Omitting both `can_buy` and `can_sell` is the documented feature-off state
  for side-tradability modeling. Providing only one, a null, or a non-boolean
  value is invalid and never defaults to tradable.
- Optional/not-computable analytics are `None`: period Sharpe with zero sample
  volatility, period Sortino with zero downside deviation, profit factor
  without losses, and payoff ratio without both wins and losses. A zero
  numerator over a valid denominator remains `0.0`. Equity and weighting
  curves must contain finite positive denominators; caller-supplied period
  returns must be finite and greater than `-1`. Invalid inputs fail explicitly
  and are never repaired with an epsilon. Missing OHLCV volume,
  prepared order quantity/limit price, broker fill price/time/fee, or persisted
  runtime facts likewise fail instead of becoming zero or an entry-price proxy.

