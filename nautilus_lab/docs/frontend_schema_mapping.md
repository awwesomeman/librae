# Frontend Schema Mapping (Canonical-First)

> Source of truth: `docs/canonical_schema.yaml` (owner: backend)
> Rule: frontend/Grafana queries **only map canonical measurement/tags/fields**; no custom field names.

## 1) Measurement / Tags / Fields → Panel Mapping

## strategy_performance
- Tags: `schema_version`, `strategy`, `symbol`, `timeframe`, `run_id`, `sample`, `benchmark`
- Fields: `total_return`, `annual_return`, `sharpe`, `max_drawdown`, `win_rate`, `trades`
- Panels:
  - Performance summary table
    - Strategy column: direct mapping from above fields
    - Benchmark column: only mapped when canonical source exists (currently total return from `perf_equity_curve.benchmark_equity` time series)

## perf_equity_curve
- Tags: `schema_version`, `strategy`, `symbol`, `timeframe`, `run_id`, `sample`, `benchmark`
- Fields: `equity`, `ret_1d`, `drawdown`, `benchmark_equity`, `benchmark_ret_1d`
- Panels:
  - Cumulative Return: Strategy vs Benchmark (`equity`, `benchmark_equity`)
  - Alpha card (derived): `strategy_performance.total_return - benchmark_total_return_from_curve`

## strategy_signals
- Tags: `schema_version`, `strategy`, `symbol`, `timeframe`, `side`, `source`, `run_id`, `signal_type`
- Fields: `signal_strength`, `confidence`, `price`, `quantity`
- Panels:
  - Asset Price + Signals (`price`, `signal_strength`, `side`)
  - Order details table (`_time`, `side`, `price`)

---

## 2) Blockers (No Workaround)

The following UI metrics previously depended on non-canonical fields and are now blockers until backend provides canonical support:

1. `profit_factor` (blocker)
2. `avg_trade_return` (blocker)
3. `exposure_ratio` (blocker)
4. `active_observations` / `total_observations` (blocker)
5. `bh_total_return`, `bh_max_drawdown`, `bh_volatility` (blocker)
6. `active_total_return`, `active_max_drawdown`, `active_volatility` (blocker)
7. `volatility` / `bh_volatility` if intended as backend-persisted KPI (blocker)

Reason: these fields are not defined in backend canonical schema (`docs/canonical_schema.yaml`) for current measurements.

---

## 3) Alignment Status

### 已對齊項
- Backend contract (`nautilus_lab/contracts.py`) required `strategy_performance` fields now aligned to canonical six fields:
  - `total_return`, `annual_return`, `sharpe`, `max_drawdown`, `win_rate`, `trades`
- Frontend query (`app/streamlit_performance.py::load_perf`) only queries canonical fields.
- Alpha/benchmark return no longer reads non-canonical `bh_total_return`; now mapped from canonical `perf_equity_curve.benchmark_equity`.

### 待 backend 補欄位
- If product still requires Profit Factor / Return Per Trade / Exposure / Active-period KPI / Benchmark KPI table columns, backend must add them into canonical schema first (with version bump and contract update), then frontend can map.

### 可先上線 MVP 面板
1. Performance summary (canonical-only)
   - Total Return, Annual Return, Sharpe, Max Drawdown, Win Rate, Trades
2. Cumulative Return: Strategy vs Benchmark
   - from `perf_equity_curve.equity` and `perf_equity_curve.benchmark_equity`
3. Asset Price + Signals
   - from `strategy_signals.price`, `strategy_signals.signal_strength`, `strategy_signals.side`
4. Order details (basic)
   - from signal timestamps/side/price mapping

---

## 4) Enforcement Notes
- Frontend must fail fast on schema mismatch (`SchemaValidationError`), not silently fallback.
- Any request to display non-canonical KPI is blocked until backend canonical schema is updated.
