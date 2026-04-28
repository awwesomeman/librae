# Backtesting & Trading Framework Selection Report

**Audience:** PM / Quant Lead
**Date:** 2026-03-24 (UTC)
**Scope:** Framework decision for short-to-mid swing, event-driven strategies, with monitoring and live-trading extension
**Revision:** v2 — updated after Lumibot PoC validation (100% signal concordance)

---

## 1) Executive Decision

### Chosen Framework: **Lumibot**

Lumibot is the unified strategy framework for both backtesting and live trading in this project.

Why:
- Event-driven Strategy class — same code path for backtest and live execution.
- 100% signal concordance validated in PoC (TrendPullback v1.0.0, BTC H1).
- Built-in broker adapters: Alpaca, IBKR, ccxt (Binance), extensible to custom brokers.
- Reasonable development velocity for solo/small team.
- Pre-computed feature injection pattern proven in PoC — compatible with existing ETL pipeline.

### Previous candidates evaluated

| Framework | Verdict |
|-----------|---------|
| NautilusTrader | Strong architecture, but high complexity overhead for current team size and strategy types. Retained as `nautilus_lab/` for reference; not the active strategy core. |
| Backtrader | Simpler but weaker production posture and live extension story. |
| VectorBT / Pro | Research accelerator only; not suitable as event-driven live engine. |
| Lean / QuantConnect | Strong institutional stack; platform lock-in concerns for self-hosted deployment. |
| Zipline | Legacy; weak live-trading extension. |

---

## 2) Architecture: Lumibot-First

### Core principle
**One strategy class, two execution modes.**

```
Strategy(lumibot.Strategy)
  |
  +-- Backtest mode: PandasDataBacktesting / YahooDataBacktesting
  +-- Live mode:     Alpaca / IBKR / ccxt (Binance) / Shioaji (custom adapter)
```

### Data pipeline
- ETL layer (`scripts/etl/`) pre-computes features (H1, D1, indicators).
- Features are injected into Lumibot Strategy via `parameters`.
- Signal schema (`poc/lumibot/signal_schema.py`) is the canonical signal contract.

### Monitoring & observability
- InfluxDB + Grafana for time-series metrics (equity, drawdown, signals).
- MLflow for experiment tracking (params, summary metrics, artifacts).
- Custom InfluxDB writer adapts Lumibot output to existing dashboard pipeline.

### Taiwan live trading (Shioaji)
- Shioaji is an optional dependency (`pip install .[tw-live]`).
- Custom Lumibot broker adapter wraps Shioaji API.
- Isolated from core to avoid CI failures on environments without Shioaji.

---

## 3) Decision Matrix (1-5, higher is better)

| Framework | Dev Cost | Live Trading | Multi-Market | Dataflow Integration | Observability | Risk Control | Community | Weighted Fit* |
|-----------|----------|-------------|-------------|---------------------|--------------|-------------|-----------|--------------|
| **Lumibot** | 4.0 | 4.0 | 3.5 | 4.0 | 3.0 | 3.5 | 3.0 | **3.64** |
| NautilusTrader | 3.0 | 4.5 | 4.0 | 4.5 | 4.0 | 4.0 | 3.5 | 4.02 |
| Lean / QC | 2.5 | 5.0 | 4.5 | 4.0 | 3.5 | 4.5 | 4.5 | 4.10 |

*Weights: Live 25%, Dataflow 15%, Observability 15%, Risk 15%, Multi-market 10%, Dev Cost 10%, Community 10%.

**Why Lumibot over higher-scoring NautilusTrader/Lean:**
- Dev Cost weight is understated for a solo/small team — actual impact is higher.
- Lumibot PoC proves the exact backtest/live parity we need with minimal code.
- NautilusTrader complexity is disproportionate to current strategy sophistication.
- Lean has platform constraints that conflict with self-hosted deployment goal.

---

## 4) When to re-evaluate

Switch away from Lumibot if:
1. Strategy complexity requires tick-level / order-book simulation (consider NautilusTrader).
2. Multi-venue atomic execution or cross-margin is needed (consider Lean or custom).
3. Lumibot maintenance stalls or breaking changes become unmanageable.
4. Team grows beyond 3 and can absorb NautilusTrader's complexity budget.
