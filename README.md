# librae

Backtest and live-trading engine, multi-asset support.

- **One strategy interface, explicit execution semantics** — `Action` and portfolio-level `RebalanceTargets` share one decision API; backtest/sim model next-bar fills, while live submits causal broker orders and books only execution reports.
- **Portfolio-level by design** — multi-asset/stock-picking needs no engine changes; `positions`/`equity_curve`/`metrics` are portfolio-level from the start.
- **Engine has no required I/O dependencies** — pure computation on a DataFrame you hand it; `db`/`brokers`/`notifications` are optional, lazy-imported, swappable via constructor injection.
- **Risk built in** — position/drawdown/volume caps, margin & liquidation simulation, volume-aware slippage — enforced at the engine level, off by default.
- **No config files required** — market/symbol cost registries are plain Python with sensible built-ins; override per run via `RunConfig`, no YAML to maintain.

`db/`, `brokers/`, `notifications/`, `orchestration/` are optional reference implementations for DB persistence, broker order routing, notifications, and CLI wiring — see [Reference implementations](#reference-implementations) below.

---

## Installing as a dependency

Not on PyPI yet — install from GitHub in the meantime:

```bash
pip install "librae @ git+https://github.com/awwesomeman/librae.git"
pip install "librae[db,crypto-live] @ git+https://github.com/awwesomeman/librae.git"   # extras work the same way
```

Same package contents as a future PyPI release (identical build), but not pinned like one: no `@<ref>` means you get the default branch's current HEAD. The version is derived from git (`setuptools_scm`), so `pip show librae`/`librae.__version__` do tell you what you got (e.g. `0.1.1.dev3+g1a2b3c4`) — but that's still a moving target without a pin. Pin a commit or tag if that matters: `...librae.git@<commit-or-tag>`.

---

## Quick Start (developing librae itself)

Supports Python 3.12 / 3.13 / 3.14 (CI runs all three).

```bash
git clone git@github-librae:awwesomeman/librae.git
cd librae
uv sync --extra test --extra dev --extra db --extra crypto-live   # add --extra tw-live/--extra us-live for brokers/'s shioaji/ib_async
cp .env.example .env   # placeholder values are enough to run the test suite
git config core.hooksPath .githooks   # runs ruff check + format --check before each commit
uv run pytest tests/ -q
```

Run everything through `uv run` afterwards, or `source .venv/bin/activate`.

### Environment variables

librae's own code (`db/`, `brokers/`, `notifications/`) reads config from env vars and never touches a `.env` file itself — loading one is the caller's job (`uv run --env-file .env ...`, direnv, your own `load_dotenv()`; `tests/conftest.py` does it for local `pytest` runs).

- **Cloned this repo?** `cp .env.example .env`. Real trading/signing secrets (`BINANCE_API_KEY`/`SHIOAJI_*`) go in a separate `.env.secrets` (`cp .env.secrets.example .env.secrets`) instead — no deploy script ever syncs that one across machines.
- **`pip install`ed, no clone?** Run `librae init` — scaffolds a minimal `.env.example` for just the vars librae's code reads (`TIMESCALE_DSN`, `TELEGRAM_*`, `BINANCE_*`, `SHIOAJI_*`, `IBKR_*`).
- **Running tests needs no real infrastructure** — `db`/`brokers` tests mock `psycopg2`/`ccxt` entirely; `TIMESCALE_DSN` just needs to be a non-empty string.

---

## Backtest engine (librae)

```python
from librae import Backtest, BaseStrategy, Action, Context, RunConfig

class MyStrategy(BaseStrategy):
    def on_bar(self, ctx: Context) -> list[Action]:
        if ctx.positions.get(ctx.symbol):
            if ctx.bar.get("exit_signal"):
                return [Action(type="close", symbol=ctx.symbol)]
            return []
        if ctx.bar.get("entry_signal"):
            return [Action(type="long", symbol=ctx.symbol)]
        return []

df = fetch_and_prepare(symbol, months)          # your own ETL, data format linked below
bt = Backtest(data=df, strategy=MyStrategy(), cfg=cfg)
bt.run()
output = bt.build_output()                      # BacktestOutput
```

Pass raw, unshifted OHLCV and point-in-time feature columns. A strategy observes
completed bar T, and the engine owns the execution delay: its intent is first
eligible on T+1. Do not pre-shift prices or signals to simulate that delay, or
the strategy will be delayed twice. The default fill is the next eligible
bar's open.

Here, **unshifted** describes timing; it does not mean adjusted or unadjusted
prices. Librae cannot infer that distinction. Execution-oriented backtests
should normally use the prices actually observable and tradable at the time
(unadjusted OHLCV). The engine does not currently book splits, dividends,
coupons, futures rolls, FX conversion, cash yield, or settlement lag. Adjusted
series may be useful for research features, but treating them as executable
OHLCV does not produce a complete cash-and-position simulation.

Backtest input is validated rather than repaired. The index must be exactly
`(symbol, datetime)`, with unique rows, timezone-aware timestamps increasing
within each symbol, and the configured symbols must exactly match the data.
`open`, `high`, `low`, `close`, and `volume` must be numeric and finite; prices
must be positive, each bar must satisfy `low <= open/close <= high`, and volume
must be non-negative. Sort, clean, and align data explicitly in ETL—the engine
does not silently reorder, fill, clip, or discard invalid observations.

`Action(fill_price=<number>)` is a one-bar limit order: buys require the next
eligible bar's low to reach the limit, sells require its high, and a gap through
the limit fills at the better opening price. If that bar does not reach the
limit, the intent expires and is logged; it is not silently carried forward.

Allocation strategies can submit a complete target portfolio without calculating quantities:

```python
from librae import RebalanceTargets

class AllocationStrategy(BaseStrategy):
    def on_bar(self, ctx: Context):
        if ctx.period_index % 24:
            return []
        return RebalanceTargets(
            weights={"BTCUSDT": 0.6, "ETHUSDT": 0.35},
            fill_price="open",
        )
```

The target is decided from bar T and filled on T+1. At execution, the engine
uses T+1 fill prices and execution-time equity, reduces positions before adding
exposure, and scales additions proportionally if costs make the batch
unaffordable. Target weights need not sum to one; the remainder stays in cash.
`RebalanceTargets.fill_price` is a bar field such as `"open"`; use per-symbol
`Action`s when different numeric limit prices are required.
With `record_position_snapshots=True`,
`output.position_snapshots` records each open position's signed market value
and realized weight after every bar, so target drift includes costs, price
movement, and execution constraints.

The configured symbol set is static, but availability is point-in-time.
`ctx.symbols` contains the configured universe while `ctx.available_symbols`
and `ctx.bars` contain only symbols with a real bar at `ctx.ts`. A symbol can
therefore start late, stop early, or miss a timestamp without stalling the
portfolio. The latest known close may value an existing position, but is never
inserted into `ctx.bars` and cannot trigger fills, stops, or holding-age
increments.

Per-symbol `Action`s become eligible independently on that symbol's next
observed bar. `RebalanceTargets` remains an atomic decision basket and waits
until every non-zero target and currently held symbol has a current bar; a
waiting basket blocks another target decision instead of being silently
replaced. Use per-symbol `Action`s when asynchronous execution is intentional.

Execution timing intentionally differs in live mode. Each batch of newly
completed bars invokes the strategy immediately; a delayed symbol may produce
a second event with the same timestamp. The current close is only a
sizing/reference price. Local quantity, entry/exit price, costs, and timestamps
are committed from the broker execution report. An acknowledgement with no
fill never opens a local position. Open and partial orders remain durable and
are polled before another strategy decision; per-symbol data watermarks prevent
completed events from being replayed after restart.

Live `Action(fill_price=None)` submits a market order and a numeric
`fill_price` submits a real broker limit order. Bar-field fills such as
`"open"`/`"close"`, `RebalanceTargets.fill_price`, and local
`stop_price`/`take_profit_price` are simulation-only: live rejects them rather
than converting a historical range touch into a late market order. Protective
orders require broker-native support. Multi-order baskets remain sequential,
not atomic.

An injected `order_adapter` implements `prepare_order`, `place_order`,
`find_order`, `get_order`, `list_open_orders`, and `cancel_order`.
`prepare_order` applies venue amount/lot/step/tick/min-notional constraints
before the request is checkpointed or submitted. Filled responses must
include order id/status, requested and cumulative filled quantity, cumulative
average price/costs, broker execution timestamp, and an explicit
fee/commission amount (zero is valid); CCXT's
`id/status/amount/filled/average/lastTradeTimestamp/fee` shape is accepted
directly (base fees are converted at average price; unrelated fee currencies
fail closed). Submitted/accepted/cancelled/rejected states are kept distinct.
Placement intent is checkpointed before network I/O; ambiguous retries recover
by deterministic client id instead of blindly resubmitting. Resting orders
block later decisions, rejected/cancelled orders halt dependent work,
halt-on-risk cancels tracked orders, and untracked open orders on configured
symbols halt for operator review.

Market-data and execution routes are separate. `data_source` selects the
default market-data adapter; it never chooses a broker. Live execution requires
either an injected `order_adapter` or an explicit `strategy.broker` (overridden
per symbol by `instrument_overrides.<symbol>.broker`). Missing execution
routing fails at startup. This prevents a symbol such as `MU` from silently
selecting IBKR merely because IBKR supplied its bars.
The current live/sim cash ledger is single-currency, so a resolved
mixed-currency universe also fails at construction until an explicit FX/base
currency model is supplied.

TimescaleDB is the default durable state store (`execution_runtime_state` plus
the `broker_orders` ledger). `cfg.no_db=True` remains valid for simulation,
but live mode then requires an explicitly injected durable `state_store`;
`MemoryLiveStateStore` is only for deterministic tests. Per-symbol processed
bar watermarks, pending intent, cash/positions, fills, equity peak, halt state,
and the active order queue restore under the same run id. Runtime-state schema
v2 is intentionally breaking: an older checkpoint is rejected and must be
explicitly migrated or removed before restart. A persisted halt
does not disappear on restart; after resolving the cause, call
`trader.reset_halt()` explicitly.

Runnable versions cover both externally scheduled allocations and dynamic
Top-K cross-sectional selection: [`examples/target_weights/`](examples/target_weights/)
and [`examples/topk_selection/`](examples/topk_selection/).

Directory layout, dependency direction, risk/margin/reconciliation/staleness details, core types, and the full Config API: [`architecture.md`'s "Backtest Engine Design"](architecture.md#backtest-engine-design-librae).

Runnable examples and what you need to know to turn on `db`/Grafana: [`examples/`](examples/).

---

## Reference implementations

`LiveTrader` injects these via constructor params (`adapter`/`order_adapter`/`cost_model`/`notifier`/`state_store`) and only lazy-imports explicitly selected built-ins when needed. `cfg.no_db=True` skips DB callbacks and the default state store; live mode must then receive a durable store explicitly.

| Directory | Injection point | Description |
|---|---|---|
| `db/` | DB callback / `state_store` | TimescaleDB analytics plus atomic runtime checkpoint/order ledger (`db/timescale_init.sql`); needs `pip install librae[db]` |
| `brokers/` | `adapter` / `order_adapter` | Shioaji (TW futures + stocks, `[tw-live]`), CCXT (crypto, `[crypto-live]`), IBKR (US stocks + futures, `[us-live]`) |
| `notifications/` | `notifier` | Telegram notifications |
| `orchestration/` | — | `cli.py`: `RunConfig` construction + CLI arg merging |

To wire your own database/broker/notifier, implement the corresponding duck-typed interface — none of these packages (or their extras) are required.

`orchestration/cli.py` is a library of helpers (`base_parser`, `parse_with_config`, `build_config`, `run_dispatch`), not an invocable command — there's no `librae backtest`/`librae run` entry point. A strategy repo calls these helpers from its own `run.py` to build a `RunConfig` and dispatch it; see any strategy's `run.py` for the pattern.

### Optional ops examples

Not engine injection points — a self-contained reference for running librae as a scheduled/VM deployment with Docker and Grafana. Use as-is, ignore, or swap for your own tooling. (Deliberately not covered in `architecture.md` — that document's scope is engine/DB architecture, not deployment.)

| Directory | Description |
|---|---|
| `deploy/` | Dockerfile, docker-compose (TimescaleDB + Grafana), VM deploy/trade scripts |
| `app/` | Grafana dashboard provisioning |
| `scripts/` | One-off ops scripts (heartbeat check, dashboard push) |

**No clone, no `deploy/`?** `LiveTrader.run()` is just a blocking polling loop — run it under whatever supervisor you already use (systemd, `pm2`, plain `docker run`, cron). For monitoring without Grafana, implement `on_heartbeat`/`on_bar` yourself and pass your own `notifier` — see [callback signatures](architecture.md#livetrader-callback-signatures-writing-your-own-db-sink-or-notifier) in `architecture.md`.

---

## Common commands

| Command | Description |
|------|------|
| `pytest tests/ -q` | Run tests |
| `ruff check .` | Lint |
| `ruff format .` | Format |

---

## Config overview

| Module | What it configures |
|------|---------|
| `librae/config/market_config.py` | Market cost/margin parameters — a small built-in registry (`crypto`/`tw_futures`/`us_equity`); inject your own via `get_market(markets={...})` |
| `librae/config/symbols.py` | symbol → market/data_source + contract multiplier — same deal, override per run (see below) |
| `db/timescale_init.sql` | DB schema for the `db/` reference example |

### Adding a market or asset

An already-registered symbol (`BTCUSDT`, `TXFR1`, `MXFR1`, `TMFR1`, `MU`, ...) needs no setup. For anything else, it's a `RunConfig` field, not a file to edit:

```python
cfg = RunConfig(
    ..., symbols=["MYSYM"], market="crypto",
    symbol_overrides={"MYSYM": {"multiplier": 1.0}},  # or e.g. 200.0 for a contract_* instrument
)
```

`symbol_overrides` is per-symbol and wins over the run-wide `cost_overrides` fallback; `spot` instruments only need `multiplier=1.0`, `contract_*` (futures) need it explicit — it varies per contract (`tw_futures`: TXF=200 vs MXF=50). For a market with a cost/margin structure `market_config.py` doesn't have, build a `MarketConfig` and pass it via `get_market(name, markets={...})` — see [`architecture.md`'s Config API](architecture.md#config-api). Only edit `symbols.py`/`market_config.py` directly if you've cloned this repo and want something registered permanently instead of repeated per run.

---

## Related documents

- [`architecture.md`](architecture.md) — system layering, engine design, naming conventions
- [`docs/decisions/`](docs/decisions/) — architecture decision records
- [`docs/plans/`](docs/plans/) — execution plans
- [`docs/learnings/ERRORS.md`](docs/learnings/ERRORS.md) — debugging log (symptom/root cause/fix/prevention)

---

## License

[MIT](LICENSE)
