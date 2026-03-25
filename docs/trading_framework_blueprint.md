# Lumibot-First Trading Architecture Blueprint (Phase 1)

## Scope and Goals

This document defines the **Phase 1 architecture** for the quant-strategy-lab, using:
- **Lumibot** as the unified strategy framework (backtest + live)
- **Binance** (crypto) data/execution via ccxt adapter
- **Shioaji** (Taiwan futures/equities) as optional live adapter (`tw-live` extra)

Constraints:
- No destructive migration of existing scripts.
- Lumibot Strategy class is the single strategy core.
- Shioaji dependency is isolated — core pipeline must work without it.

---

## 1) Current Architecture

### Active code paths

| Path | Role |
|------|------|
| `poc/lumibot/strategy_lumibot.py` | **Primary** — Lumibot Strategy implementation |
| `poc/lumibot/signal_schema.py` | Canonical signal contract |
| `poc/lumibot/sample_data.py` | Synthetic data generator |
| `poc/lumibot/reference_signals.py` | Reference signal extractor for concordance |
| `poc/lumibot/compare_signals.py` | Signal concordance tooling |
| `scripts/etl/core_features.py` | Feature engineering (resampling, indicators) |
| `scripts/etl/core_data_sources.py` | Binance/data fetch with retry |
| `scripts/etl/cache_store.py` | File cache for ETL idempotency |
| `scripts/monitor/` | Monitoring utilities (state, logging, dedupe) |
| `scripts/strategies/` | Legacy strategy scripts (reference only) |

### Reference only (not active strategy core)

| Path | Role |
|------|------|
| `nautilus_lab/` | NautilusTrader-based architecture (retained for reference) |

---

## 2) Design Principles

1. **One strategy class, two modes** — same Lumibot Strategy for backtest and live.
2. **Pre-computed feature injection** — ETL pipeline feeds features via `parameters`.
3. **Adapter pattern for brokers** — Lumibot broker adapters for Alpaca/ccxt/IBKR; custom adapter for Shioaji.
4. **Signal contract** — `Signal` dataclass is the canonical output format.
5. **Progressive migration** — wrap existing scripts, don't rewrite.

---

## 3) Data Pipeline

### Historical (backtest)
1. ETL scripts fetch raw OHLCV (Binance REST / cached files).
2. `core_features.py` computes indicators (EMA, ATR, volume SMA).
3. Features injected into Lumibot Strategy parameters.
4. Lumibot `PandasDataBacktesting` engine runs strategy.
5. Signals and metrics persisted.

### Live
1. Lumibot broker adapter receives live market data.
2. Strategy receives bars via `on_trading_iteration()`.
3. Pre-computed features updated via external ETL or internal calculation.
4. Signals emitted, orders placed through broker adapter.

### Persistence
- Raw: partitioned parquet/jsonl (audit/replay).
- Metrics: InfluxDB (equity curve, drawdown, signals).
- Experiments: MLflow (params, summary, artifacts).

---

## 4) Backtest vs Live Path

### Backtest
```
ETL -> features -> Lumibot Strategy (PandasDataBacktesting) -> signals + metrics
```

### Live
```
Broker adapter -> Lumibot Strategy (live) -> signals -> order execution -> metrics
```

**Parity rule:** Strategy class is identical. Only the data source and execution adapter differ.

---

## 5) Dependency Layers

```
core (default install):
  lumibot, pandas, numpy, empyrical, influxdb-client, httpx

tw-live (optional):
  shioaji

test:
  pytest, pytest-asyncio
```

---

## 6) Risk & Execution (Phase 1 Skeleton)

### Pre-trade risk
- Max position per instrument.
- Daily loss cap and kill switch.
- Order frequency throttle.

### Execution
- Lumibot handles order routing to broker adapter.
- Slippage tracking vs reference price post-fill.

---

## 7) Monitoring & Observability

### Metrics
- Ingestion lag, signal throughput, strategy decision latency.
- Order ack/fill latency, reject/cancel ratio, slippage bps.
- PnL, drawdown, exposure.

### Stack
- InfluxDB + Grafana (time-series dashboards).
- Structured JSON logging with `run_id`, `strategy_id`, `instrument_id`.

---

## 8) Minimal Directory Structure

```
quant-strategy-lab/
  pyproject.toml              # core + tw-live + test extras
  poc/lumibot/                # Lumibot strategy implementations
    strategy_lumibot.py       # TrendPullback Lumibot Strategy
    signal_schema.py          # Canonical signal contract
    sample_data.py            # Synthetic data generator
    reference_signals.py      # Reference extractor
    compare_signals.py        # Concordance comparison
  scripts/
    etl/                      # Feature engineering & data fetch
    monitor/                  # Monitoring utilities
    strategies/               # Legacy strategy scripts (reference)
  nautilus_lab/               # NautilusTrader reference (not active core)
  tests/                      # All tests
  docs/                       # Architecture docs
  .github/workflows/          # CI pipelines
```

---

## 9) Executable Backlog

### P0 (current)
1. Root `pyproject.toml` with dependency layers.
2. CI split: core tests + tw-live tests.
3. Signal concordance test in CI gate.
4. InfluxDB seed pipeline for backtest results.

### P1
1. Promote `poc/lumibot/` to production module structure.
2. MLflow experiment tracking integration.
3. Grafana dashboard v0.

### P2
1. Custom Shioaji Lumibot broker adapter.
2. Live paper-trading pipeline.
3. Scheduling and notification system.
