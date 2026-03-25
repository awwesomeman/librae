# quant-strategy-lab Implementation Plan

> Updated: 2026-03-24
> Framework: **Lumibot-first** (backtest + live, single strategy class)
> Status: Executing

---

## 1) Goal Alignment

### End goal
Signal subscription platform covering futures, crypto, pair trading, stock selection.

### Current phase goal
Single-asset, verifiable, monitorable MVP:
1. Lumibot strategy produces signals with real/synthetic data
2. Compute key performance metrics
3. Backtest dashboard (Grafana + Streamlit)
4. Monitoring dashboard
5. Validate strategy correctness (signal concordance >= 95%)
6. Extensible to multi-asset / advanced strategies

---

## 2) Tech Stack

| Area | Tool | Rationale |
|------|------|-----------|
| **Strategy framework** | **Lumibot** | Unified backtest + live; PoC validated 100% concordance |
| Time-series storage | InfluxDB 2.x | Equity curve / signal / drawdown / monitoring metrics |
| Experiment tracking | MLflow | Params, summary metrics, artifacts, version comparison |
| API | FastAPI | Service layer with Pydantic contracts |
| Dashboards | Grafana + Streamlit | Grafana for monitoring/alerts; Streamlit for backtest analysis |
| Alerts | Grafana Alerting (Telegram) | Minimal custom alert complexity |
| Scheduling | cron -> Prefect | Start simple, upgrade when needed |
| Testing/CI | pytest + GitHub Actions | Regression, contract, integration test gates |
| Deployment | docker-compose | MVP fast landing; scale later |
| TW live trading | Shioaji (optional extra) | Isolated `tw-live` dependency group |

### What about NautilusTrader?
`nautilus_lab/` remains in the repo as reference architecture and for advanced use cases.
It is NOT the active strategy execution layer. Lumibot is.

---

## 3) Phase 0~3 Execution Plan

### Phase 0 (1.5~2 weeks): Foundation & E2E pipeline

**Goal:** One single-asset strategy completes full chain:
Lumibot backtest -> metrics -> InfluxDB -> Grafana.

**Deliverables:**
1. `docker-compose` for InfluxDB + Grafana
2. Canonical schema (measurement/tag/field)
3. Lumibot backtest output aligned to `BacktestOutput` (with `run_id`)
4. Seed script: backtest results -> InfluxDB
5. Grafana basic panels (equity, drawdown, win rate)
6. CI smoke + contract tests

**Acceptance:**
- New env reproducible in 5 min
- Grafana shows `run_id` curves
- Schema validation passes
- CI green

---

### Phase 1 (2~3 weeks): Experiment tracking & strategy comparison

**Goal:** Comparable, reproducible strategy experiment workflow.

**Deliverables:**
1. MLflow server + run log integration
2. Auto-log: params, summary metrics, artifacts per backtest
3. At least 2 strategies comparable (TrendPullback / MultiFactor)
4. Streamlit analysis page
5. Parity test v0 (legacy vs Lumibot output)

---

### Phase 2 (3~4 weeks): Scheduling, notifications, API

**Goal:** Strategies run on schedule with external query and notifications.

**Deliverables:**
1. cron / Prefect MVP for scheduled backtest/monitor
2. Telegram signal push
3. Grafana alert rules (drawdown, heartbeat)
4. FastAPI skeleton (`/health`, `/signals/{strategy}`)
5. Retry + basic observability (log + metric)

---

### Phase 3 (6~8 weeks): Multi-asset & subscription platform

**Goal:** From research tool to externally serviceable platform.

**Deliverables:**
1. Multi-asset strategy support (futures/crypto/pair/selection)
2. User & subscription data model (PostgreSQL)
3. Auth (JWT)
4. Subscription management & notification routing
5. Versioned strategy release process

---

## 4) When to refactor

Trigger major refactor when 2-3 of these are true:
1. Strategy count > 10 with > 40% duplicated logic
2. Maintenance cost consistently > 1.5x baseline
3. Need tick/orderbook-level backtest fidelity
4. Subscribers > 50 and API latency is a bottleneck
5. Data sources > 3 causing adapter layer inconsistency

Refactor direction: consolidate all strategy logic into Lumibot Strategy classes.

---

## 5) 30 / 60 / 90 Day Milestones

### Day 30
- Phase 0 complete
- One strategy E2E reproducible (Lumibot backtest -> InfluxDB -> Grafana)
- Schema + run_id contract locked

### Day 60
- Phase 1 complete, Phase 2 started
- MLflow comparison for >= 2 strategies
- Streamlit analysis page live
- Parity test in CI

### Day 90
- Phase 2 complete
- Scheduling + push + API working
- Monitoring alerts stable
- Data available for Phase 3 (platform) go/no-go decision

---

## 6) Immediate Next Actions

1. Create root `pyproject.toml` with `core` and `tw-live` extras
2. Set up CI split: core tests (no shioaji) + tw-live tests (optional)
3. Promote `poc/lumibot/` patterns to production structure
4. Build InfluxDB seed pipeline (backtest results -> InfluxDB)
5. Build Grafana v0 dashboard (equity/drawdown/trade count)
6. Establish Phase 0 CI gate (smoke + contract + signal concordance)

---

## 7) Principles

- Every phase must be demo-able and acceptance-testable
- Don't refactor prematurely for "looking advanced"
- Usable first, extensible second; correct first, optimized second
- Lumibot is the single source of truth for strategy execution
