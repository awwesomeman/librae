# Nautilus MVP Infrastructure — Implementation Plan

**Last updated:** 2026-03-06
**Schema version:** 1.0.0

---

## Overview

This plan covers the two-week delivery of the Nautilus MVP infrastructure skeleton:
- Unified adapter abstraction layer (market_data / order / account)
- Standardized backtest output schema + persistence
- Minimal monitoring skeleton (InfluxDB)
- Docs and tests

---

## Week 1 — Skeleton + Contracts (Days 1–7)

### Day 1–2: Adapter abstraction layer
- [x] Define `AdapterInfo`, `L1Quote`, `TradeTick`, `Bar`, `Order`, `Fill`, `Position` dataclasses
- [x] Define `MarketDataAdapter`, `OrderAdapter`, `AccountAdapter` ABCs in `nautilus_lab/adapters/base.py`
- [x] Implement `BinanceMarketDataAdapter`, `BinanceOrderAdapter`, `BinanceAccountAdapter` stubs
- [x] Implement `ShioajiMarketDataAdapter`, `ShioajiOrderAdapter`, `ShioajiAccountAdapter` stubs
- [ ] Wire Binance stubs to ccxt (REST + WS)
- [ ] Wire Shioaji stubs to shioaji SDK

### Day 3–4: Backtest output schema + persistence
- [x] Define `RunMetadata`, `EquityCurvePoint`, `TradeRecord`, `StrategyMetrics`, `BacktestOutput` in `nautilus_lab/backtest/schema.py`
- [x] Implement `save_backtest_output` / `load_backtest_output` (JSON + equity CSV) in `nautilus_lab/backtest/persistence.py`
- [ ] Integrate with NautilusTrader backtest engine output

### Day 5–7: Monitoring skeleton
- [x] Define `MonitoringConfig`, `build_strategy_point`, `build_system_point` pure helpers
- [x] Implement `StrategyMetricsSender` and `SystemMetricsSender` async context managers
- [ ] Wire system resource collection (cpu/mem via psutil)
- [ ] Add periodic heartbeat loop

---

## Week 2 — Wiring + Integration (Days 8–14)

### Day 8–9: Adapter wiring — Binance
- [ ] Implement `BinanceMarketDataAdapter.fetch_bars` via ccxt OHLCV
- [ ] Implement `BinanceMarketDataAdapter.subscribe_l1` via ccxt WS BBO stream
- [ ] Implement `BinanceOrderAdapter.submit_order` / `cancel_order` / `get_order`
- [ ] Implement `BinanceAccountAdapter.get_positions` / `get_balance`

### Day 10–11: Adapter wiring — Shioaji
- [ ] Implement `ShioajiMarketDataAdapter.fetch_bars` via `sj.Contracts` + `sj.get_snapshots`
- [ ] Implement `ShioajiMarketDataAdapter.subscribe_l1` via `sj.subscribe`
- [ ] Implement `ShioajiOrderAdapter.submit_order` via `sj.place_order`
- [ ] Implement `ShioajiAccountAdapter.get_positions` via `sj.list_positions`

### Day 12: Backtest integration
- [ ] Produce `BacktestOutput` from NautilusTrader `BacktestEngine` results
- [ ] Auto-save equity CSV to `data/reports/<run_id>_equity_curve.csv`
- [ ] Expose reports directory to Streamlit `app/streamlit_performance.py`

### Day 13: Grafana / Streamlit validation
- [ ] Verify equity curve CSV loads correctly in Streamlit
- [ ] Verify `strategy_performance` points visible in Grafana
- [ ] Verify `system_heartbeat` / `system_resources` visible in Grafana

### Day 14: Hardening
- [ ] Add integration test for Binance adapter (sandbox/testnet)
- [ ] Add integration test for Shioaji adapter (paper account)
- [ ] CI gate: `pytest tests/` must pass before merge

---

## Module Map

```
nautilus_lab/
├── adapters/
│   ├── base.py          # ABCs and canonical data types
│   ├── binance.py       # Binance stubs (→ ccxt)
│   └── shioaji.py       # Shioaji stubs (→ shioaji SDK)
├── backtest/
│   ├── schema.py        # BacktestOutput, RunMetadata, EquityCurvePoint, TradeRecord, StrategyMetrics
│   └── persistence.py   # save/load JSON + CSV
├── monitoring/
│   └── metrics.py       # build_strategy_point, build_system_point, senders
├── contracts.py         # InfluxDB measurement schema, validation helpers
└── influx_actor.py      # NautilusTrader Actor (trade ticks + positions)
```

---

## Contract Rules

| Rule | Details |
|------|---------|
| snake_case | All field names, measurement names, tag/field keys |
| Explicit units | Every quantity has a companion `*_unit` field |
| No units in keys | `pnl_unit: "TWD"` — NOT `pnl_twd: 123.0` |
| Cost/slippage reserved | `commission`, `slippage` fields exist but may be `None` |
| Schema version tagged | All InfluxDB measurements tag `schema_version` |
| Run ID required | All backtest outputs carry a `run_id` |

---

## Blockers / Open Items

- [ ] Binance API key / sandbox credentials not yet configured
- [ ] Shioaji account credentials not yet configured
- [ ] InfluxDB instance must be running for monitoring senders to write
- [ ] psutil not in `pyproject.toml` (needed for cpu/mem collection)
- [ ] NautilusTrader backtest engine adapter not yet wired to `BacktestOutput`
