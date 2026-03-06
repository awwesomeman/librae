# Backtesting & Trading Framework Selection Report

**Audience:** PM / Quant Lead  
**Date:** 2026-03-06 (UTC)  
**Scope:** Framework decision for short-to-mid swing, event-driven strategies, with monitoring and live-trading extension

---

## 1) Executive Decision

### Default Choice (Recommended): **NautilusTrader**

NautilusTrader is the best default fit **if the target architecture is event-driven from day 1 and must extend to live trading + monitoring without strategy rewrite**.

Why:
- Event-driven engine + same strategy path for backtest/live.
- Production-oriented architecture for multi-venue and adapter-based data/execution integration.
- Better long-term alignment with your current direction (event-based, monitoring-heavy, eventual real deployment) than research-only stacks.

### Decision in one line
Use **NautilusTrader as core execution/backtest engine**, and keep **VectorBT( Pro ) as optional research accelerator**, not as core live engine.

---

## 2) Requirement Fit (Current Direction)

Target requirements:
1. Short-to-mid swing (not HFT, but still path-dependent and timing-sensitive)
2. Event-driven strategy logic
3. Monitoring / observability required
4. Live-trading extensibility required
5. Reasonable solo-dev build speed and maintenance

### Is NautilusTrader the most suitable?
**Yes, for this requirement set.**

It is not the cheapest to start, but it has the highest architectural alignment with the requested end-state (event-driven + live + monitoring continuity).

---

## 3) Alternatives Compared

Compared options:
- NautilusTrader
- Backtrader
- VectorBT / VectorBT Pro
- Lean / QuantConnect
- Zipline
- Custom in-house event engine

### High-level trade-offs

- **Backtrader:** easiest legacy Python event framework to start, but slower modernization and weaker production posture.
- **VectorBT/Pro:** strongest for rapid vectorized research and parameter sweeps, but not ideal as primary event-driven live engine.
- **Lean/QuantConnect:** very strong institutional stack and brokerage breadth; excellent when you accept platform constraints/complexity.
- **Zipline:** good educational/research heritage; weaker live-trading extension story.
- **Custom engine:** max flexibility, but largest delivery and maintenance risk for a small team.

---

## 4) Decision Matrix (1-5, higher is better)

> Criteria requested: development cost, live ability, multi-market support, dataflow integration, observability, risk controls, community/maintenance.

| Framework | Dev Cost (speed) | Live Trading | Multi-Market | Dataflow Integration | Observability | Risk Control Extensibility | Community / Maintenance | Weighted Fit* |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **NautilusTrader** | 3.0 | 4.5 | 4.0 | 4.5 | 4.0 | 4.0 | 3.5 | **4.02** |
| Backtrader | 4.0 | 3.0 | 3.0 | 3.0 | 2.5 | 3.0 | 3.0 | 3.08 |
| VectorBT / Pro | 4.5 | 1.5 | 3.5 | 3.5 | 2.5 | 2.0 | 4.0 | 2.91 |
| Lean / QuantConnect | 2.5 | 5.0 | 4.5 | 4.0 | 3.5 | 4.5 | 4.5 | 4.10 |
| Zipline | 3.0 | 1.5 | 2.5 | 2.5 | 2.0 | 2.5 | 2.5 | 2.27 |
| Custom event engine | 1.5 | 4.0 | 4.5 | 5.0 | 5.0 | 5.0 | 1.0 | 3.93 |

\*Weighted Fit weights (for this project): Live 25%, Dataflow 15%, Observability 15%, Risk 15%, Multi-market 10%, Dev Cost 10%, Community 10%.

### Interpretation
- Lean scores highest on paper due to maturity + broker breadth, but has higher platform/process overhead and potential lock-in concerns depending on deployment mode.
- NautilusTrader has the best **self-hosted event-driven balance** for current roadmap.
- Custom engine can score high technically, but is usually a delivery trap for a small team.

---

## 5) Recommendation Policy

## Default selection
**Choose NautilusTrader** as the primary framework for next 6-12 months.

## When to switch away from NautilusTrader
Switch to **Lean/QuantConnect** if one or more is true:
1. You need broad, ready-made brokerage/asset coverage immediately.
2. Team values managed platform reliability over local architecture control.
3. You require fastest path to multi-asset institutional workflows and accept Lean’s complexity.

Switch to **Backtrader** only if:
1. Objective is quick educational prototype with minimal engineering.
2. Live-trading ambitions are limited/temporary.

Use **VectorBT/Pro as primary** only if:
1. The project is research-first (factor/signal exploration),
2. Event-driven execution realism is secondary,
3. Live deployment is handled by a separate execution layer.

Choose **custom engine** only if:
1. Hard requirements cannot be met by existing frameworks,
2. You can fund long-term infrastructure ownership,
3. You accept higher model/ops risk and slower alpha iteration.

---

## 6) Migration Cost & Risk

### If starting with NautilusTrader (recommended path)
- **Near-term cost:** medium (adapter/data model/ops setup)
- **Mid-term benefit:** avoids dual-stack rewrite from backtest to live
- **Main risks:** integration maturity variance across venues; learning curve for architecture patterns

### Migration scenarios

1. **NautilusTrader -> Lean**
   - Cost: medium-high
   - Risk: strategy API/event model rewrite, infra/tooling mismatch

2. **Backtrader/Zipline -> NautilusTrader**
   - Cost: medium-high
   - Risk: refactor from simpler strategy lifecycle to production event abstractions

3. **VectorBT-first -> NautilusTrader execution**
   - Cost: medium
   - Risk: signal semantics drift between vectorized assumptions and event-driven fill logic

4. **Custom -> anything else**
   - Cost: very high
   - Risk: bespoke abstractions create lock-in to your own codebase

---

## 7) Practical Implementation Path (PM-friendly)

### Phase 0 (1-2 weeks): Decision lock + architecture guardrails
- Lock core engine: NautilusTrader
- Define canonical strategy I/O schema (signals, orders, fills, pnl, risk events)
- Define observability contract (Influx/Grafana metrics naming, run_id, sample tags)

### Phase 1 (2-4 weeks): Minimal production-grade baseline
- Implement one strategy end-to-end (backtest -> report -> paper/live-sim)
- Add mandatory risk controls (max loss/day, position cap, kill-switch)
- Add monitoring panels (equity, drawdown path, order rejects, latency, exposure)

### Phase 2 (4-8 weeks): Robustness and scaling
- Add 2nd venue/asset adapter path
- Add replay tests and failure drills (disconnect, stale data, partial fills)
- Establish release checklist for strategy promotion (research -> final -> live)

---

## 8) Final PM Decision Statement

For your stated direction, **NautilusTrader should be the default framework**.

- It best matches event-driven strategy development with a realistic path to live trading and monitoring.
- Keep VectorBT/Pro as research-side acceleration where needed.
- Reconsider Lean only when immediate broker/asset breadth and managed deployment outweigh architecture control.

This choice optimizes for **strategic continuity** (same conceptual model from research to production) and reduces hidden rewrite risk in later stages.

---

## Appendix: Evidence snapshots (public documentation)

- NautilusTrader docs: “event-driven engine” and “deploy same strategies live with no code changes”; modular integrations/adapters list.
- Backtrader docs/site: feature-rich backtesting/trading framework; live integration examples (e.g., Interactive Brokers).
- VectorBT docs: vectorized NumPy/Pandas + Numba approach for high-speed strategy analysis/optimization.
- QuantConnect LEAN docs: algorithm engine architecture and broad brokerage/live support.
- Zipline docs: event-driven backtesting lineage and community-maintained continuation.
