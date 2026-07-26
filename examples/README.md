# Examples

`simple_sma/` is a minimal, runnable strategy showing how a strategy repo
wires itself to librae. Try it:

```bash
uv run python -m examples.simple_sma.run --mode backtest --no-db
uv run python -m examples.simple_sma.run --mode sim --poll-seconds 5 --no-db   # Ctrl+C to stop
```

librae's engine and reference implementations (`db/`, `brokers/`,
`notifications/`) are all optional and independently pluggable — but a few
things only make sense once you actually turn one on, and aren't librae's
job to explain in `architecture.md` (engine/DB scope only) or the ops
README section (deployment only). This file is that gap, consolidated.

## 1. What shape does the backtest engine expect?

`Backtest(data=...)` requires a `MultiIndex(symbol, datetime)` DataFrame with
OHLCV columns, **already featured** (your strategy's signal columns computed
in advance — the engine doesn't compute features itself). See
`examples/simple_sma/run.py::run_backtest` for the exact construction:

```python
df.index = pd.MultiIndex.from_arrays(
    [[symbol] * len(df), df.index], names=["symbol", "datetime"]
)
```

Live mode is different: `LiveTrader` calls your `prepare_signals(df)` itself,
once per new bar, on a plain (non-MultiIndex) OHLCV DataFrame — which is why
`simple_sma/strategy.py` writes `prepare_signals` to accept either shape.
Reusing the same function for both modes isn't a suggestion — it's how you
avoid backtest/live computing the signal differently.

## 2. Using `db/` — do you need to know the schema?

Only if you write to it directly. The normal path is `db.timescale_writer`'s
high-level functions (`save_strategy_results`, `write_equity_curve_point`,
...) — see `run_backtest` in this example for where that call would go.

You do need the schema (`db/timescale_init.sql`) to **create the tables**
once per database:

```bash
psql "$TIMESCALE_DSN" -f db/timescale_init.sql
```

Not using a database at all? Pass `--no-db` (as both commands above do) —
`cfg.no_db=True` skips every DB write, no Postgres/TimescaleDB required.

## 3. Using the Grafana dashboards — what do you need to know?

`app/grafana/generate_dashboards.py` generates dashboard JSON that Grafana
auto-loads via file provisioning (`app/grafana/provisioning/`) — you don't
import or call anything from `app/` yourself. But the dashboards are just
queries over the `db/` tables above, so:

- **They show nothing until `db/` actually has data in it.** Run a backtest
  without `--no-db` (schema created, `TIMESCALE_DSN` set) to populate
  `backtest_runs`/`equity_curve`/`trade_events`/`signal_events`.
- **Want to see a populated dashboard without running a real strategy
  first?** `db/seed_fake_data.sql` inserts one fake row per table:
  `psql "$TIMESCALE_DSN" -f db/seed_fake_data.sql`.
- Deploying Grafana itself (Docker, provisioning paths) is covered by the
  root [README's "Optional ops examples"](../README.md#optional-ops-examples)
  — this section is only about what feeds the dashboards, not how to stand
  Grafana up.
