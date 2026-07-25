# librae

Backtest and live-trading engine. `db/`, `brokers/`, `notifications/`, `orchestration/` are optional reference implementations for DB persistence, broker order routing, notifications, and CLI wiring — `librae` itself never hard-depends on any of them.

---

## Quick Start (local)

Supports Python 3.12 / 3.13 / 3.14 (CI runs all three, see `.github/workflows/core-tests.yml`).

```bash
git clone git@github-librae:awwesomeman/librae.git
cd librae
uv sync --extra test --extra dev --extra db --extra crypto-live   # for dev/tests; add --extra tw-live/--extra us-live only if you need brokers/'s shioaji/ib_async
git config core.hooksPath .githooks   # runs ruff check + format --check before each commit
```

Run everything through `uv run` afterwards (e.g. `uv run pytest tests/ -q`), or `source .venv/bin/activate` and run commands directly.

### Environment variables

librae's own code (`db/`, `brokers/`, `notifications/`) reads config from env vars — it never reads a `.env` file itself; loading one is the caller's job (`uv run --env-file .env ...`, direnv, or your own `load_dotenv()` call).

- **Cloned this repo?** `cp .env.example .env` at the repo root — this template also covers the `deploy/` reference examples below (docker-compose, Grafana). Secrets with real trading/signing power (`BINANCE_API_KEY`/`SHIOAJI_*`) live in a separate `.env.secrets` (`cp .env.secrets.example .env.secrets`), which no deploy script ever syncs across machines.
- **`pip install librae` only, no clone?** Run `librae init` — it scaffolds a minimal `.env.example` covering just the variables librae's own code reads (`TIMESCALE_DSN`, `TELEGRAM_*`, `BINANCE_*`, `SHIOAJI_*`, `IBKR_*`), with no docker-compose/Grafana-specific settings.

---

## Backtest engine (librae)

Backtest and live-trading engine. Provides a full framework for strategy execution, position management, cost simulation, and performance metrics — **backtest, sim, and live trading share the exact same strategy code, unmodified.**

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

For the engine's directory layout, dependency direction, risk/margin/reconciliation/staleness-detection details, core types, design decisions, and the full Config API, see [`architecture.md`'s "Backtest Engine Design"](architecture.md#backtest-engine-design-librae).

---

## Reference implementations

The engine itself never imports these packages — `LiveTrader` injects them via constructor params (`adapter`/`order_adapter`/`cost_model`/`notifier`), skips them entirely under `cfg.no_db=True`, and only lazy-imports the defaults below when nothing is injected.

| Directory | Injection point | Description |
|---|---|---|
| `db/` | DB write callback | TimescaleDB read/write; schema in `db/timescale_init.sql`, sample data in `db/seed_fake_data.sql`; needs `pip install librae[db]` |
| `brokers/` | `adapter` / `order_adapter` | Shioaji (Taiwan futures, `[tw-live]`), CCXT (crypto, `[crypto-live]`), IBKR (`[us-live]`) adapters |
| `notifications/` | `notifier` | Telegram notifications |
| `orchestration/` | — | `cli.py`: `RunConfig` construction + CLI arg merging; a reference for wiring the three above into the engine |

To wire your own database/broker/notifier, just implement the corresponding duck-typed interface — none of these packages are required, and their dependencies (`psycopg2-binary`, `ccxt`, `shioaji`, `ib-async`) are all optional extras, not base installs.

### Optional ops examples

These aren't engine injection points — they're a self-contained reference for running librae as a scheduled/VM deployment with Docker and Grafana. Use them as-is, ignore them, or swap in your own tooling.

| Directory | Description |
|---|---|
| `deploy/` | Dockerfile, docker-compose (TimescaleDB + Grafana), and VM deploy/trade scripts (`cloud_deploy.sh`, `trade.sh`, `build_push.sh`) |
| `app/` | Grafana dashboard provisioning (datasources, dashboard JSON, `generate_dashboards.py`) |
| `scripts/` | One-off ops scripts (heartbeat check, dashboard push) |

### Deploying without `deploy/`

`deploy/`'s docker-compose/Grafana stack assumes a clone of this repo. If you're using librae as a `pip install`ed library instead, there's no separate deployment story to learn — `LiveTrader.run()` is just a blocking polling loop, so running it in production means putting that process under whatever supervisor you already use (a systemd unit, `pm2`, a bare `docker run`, or a cron job for periodic sim checks) — nothing librae-specific about it.

Monitoring works the same way: without Grafana, implement `on_heartbeat`/`on_bar` yourself (a log line, a Prometheus pushgateway call, a health-check file — whatever your stack already reads) and pass your own `notifier` for alerts instead of Telegram. See ["LiveTrader callback signatures"](architecture.md#livetrader-callback-signatures-writing-your-own-db-sink-or-notifier) in `architecture.md` for the exact callback signatures to implement.

---

## Common commands

| Command | Description |
|------|------|
| `pytest tests/ -q` | Run tests |
| `ruff check .` | Lint (scope defined in `pyproject.toml`'s `[tool.ruff]`) |
| `ruff format .` | Format |

---

## Config file overview

| File | What it configures | Tracked in git |
|------|---------|-----------|
| `librae/config/markets.yaml` | Market cost + margin parameters (can also be injected externally, bypassing this file — see `get_market(markets=)`) | yes |
| `librae/config/symbols.yaml` | symbol → market/data_source mapping | yes |
| `db/timescale_init.sql` | DB schema (for the `db/` reference example) | yes |

---

## Related documents

- [Backtest engine (librae)](#backtest-engine-librae) (this document) — engine architecture, API, type system
- [`architecture.md`](architecture.md) — system layering, naming conventions
- [`docs/decisions/`](docs/decisions/) — architecture decision records
- [`docs/plans/`](docs/plans/) — execution plans
- [`docs/learnings/ERRORS.md`](docs/learnings/ERRORS.md) — debugging log (symptom/root cause/fix/prevention)
