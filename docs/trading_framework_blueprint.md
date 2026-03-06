# NautilusTrader + Multi-Source Data Integration Blueprint (Phase 1)

## Scope and Goals

This document defines a **Phase 1, implementation-oriented architecture** for integrating:
- **Binance** (crypto) data/execution flows
- **Shioaji (Sinopac/永豐)** (Taiwan futures/equities) data/execution flows
- **NautilusTrader** as the default strategy/runtime framework

Constraints applied:
- No destructive migration.
- Keep current scripts operational.
- Prioritize architectural convergence, contracts, and backlog.

---

## 1) Legacy Scan: Current Reusable Assets, Technical Debt, and Risks

## 1.1 Discovery summary

### Active code paths (current workspace)
- `scripts/etl/core_data_sources.py`
- `scripts/etl/cache_store.py`
- `scripts/monitor/monitor_core.py`
- `scripts/monitor/monitor_run.py`
- `scripts/monitor/utils_state.py`
- `scripts/monitor/utils_logging.py`
- `scripts/monitor/utils_dedupe.py`
- `scripts/monitor/monitor_profiles/*.json`
- `scripts/backtest/run_backtest.py`
- `nautilus_lab/nautilus_lab/influx_actor.py`
- `nautilus_lab/docs/l1_data_contract.md`

### Legacy/archive references
- `archive/20260303/legacy/*.py` (Binance and Shioaji one-off scripts)
- `archive/20260303/*.py` (pre-refactor monitoring/backtest scripts)

---

## 1.2 Reusable modules (keep and wrap)

1. **Binance historical fetch + retry/backoff + pacing**
   - File: `scripts/etl/core_data_sources.py`
   - Reuse:
     - `_binance_request()` with retry on 429/5xx
     - `_pace()` and chunking logic
     - `fetch_binance_spot_klines()`, `fetch_binance_futures_klines()`

2. **Data normalization baseline (OHLCV)**
   - File: `scripts/etl/core_data_sources.py`
   - Reuse:
     - `normalize_ohlcv()` standardization pattern

3. **File-cache utility for ETL idempotency**
   - File: `scripts/etl/cache_store.py`
   - Reuse:
     - deterministic cache keys
     - atomic write (`tmp + os.replace`)

4. **Monitoring state/logging primitives**
   - Files:
     - `scripts/monitor/utils_state.py`
     - `scripts/monitor/utils_logging.py`
     - `scripts/monitor/utils_dedupe.py`
   - Reuse:
     - JSON state persistence
     - JSONL append + log rotation
     - signal dedupe key convention

5. **L1 monitoring data contract direction**
   - File: `nautilus_lab/docs/l1_data_contract.md`
   - Reuse:
     - Measurement/tag/field conventions for dashboards

6. **Nautilus-adjacent observability actor**
   - File: `nautilus_lab/nautilus_lab/influx_actor.py`
   - Reuse:
     - asynchronous export pattern to InfluxDB

---

## 1.3 Technical debt

1. **Duplicated business logic between monitoring and ETL layers**
   - Similar OHLCV transform/indicator logic exists in multiple places (`monitor_core.py`, legacy scripts).

2. **Tight coupling of strategy logic and data access**
   - `monitor_run.py` mixes data fetch, strategy trigger, state mutation, and event publishing.

3. **Inconsistent time handling**
   - Some paths enforce UTC explicitly; others rely on default datetime parsing.

4. **Partial error handling asymmetry**
   - Binance path includes retry/backoff in ETL helpers.
   - Shioaji path often returns empty frame or direct login/fetch/logout without robust reconnect policy.

5. **State and schema fragmentation**
   - State in JSON files, logs in JSONL, metrics in Influx; contracts are not fully unified in one canonical schema package.

6. **Legacy scripts remain executable but unmanaged**
   - Archive scripts are useful references but can be accidentally reused in production-like flow.

---

## 1.4 Risk register (Phase 1)

1. **Credential and account risk (Shioaji)**
   - Environment keys are required; operational mistakes may route to unintended account modes.

2. **Data quality risk**
   - Symbol mapping, timezone conversion, and contract rollover handling (especially futures front-month) are not yet centrally enforced.

3. **Rate-limit and API availability risk**
   - Binance has guardrails in one path, but not uniformly in all data consumers.

4. **Single-point runtime script risk**
   - One script failure in monitor path can stop signal flow due to limited supervisor/recovery workflow.

5. **Backtest/live parity risk**
   - Strategy logic in script-based backtest may diverge from future Nautilus live execution unless unified behind common domain model.

---

## 2) Integration Architecture Blueprint

## 2.1 Design principles

- **Single normalization contract** for market data/events.
- **Adapter pattern** for exchange/broker-specific details.
- **Event-driven internal bus** to decouple ingestion, strategy, risk, and execution.
- **Same strategy core** for backtest and live, with only adapters switched.
- **Progressive migration** (strangler pattern): wrap first, replace later.

---

## 2.2 Data Ingestion (historical/live)

### Historical
- Binance:
  - Keep current REST fetch modules as adapter implementation.
  - Add ingestion job wrapper for batch windows and persistence targets.
- Shioaji:
  - Add dedicated historical adapter wrapper around current kbars pull.
  - Add retry/re-login policy and empty-data diagnostics.

### Live
- Binance:
  - Add websocket adapter for ticks/book updates.
- Shioaji:
  - Implement `ShioajiDataClient`/bridge adapter (callback -> normalized event).
  - Non-blocking callback to queue handoff.

### Persistence targets
- Raw immutable store: partitioned parquet/jsonl (audit/replay).
- Normalized store: parquet + optional TSDB for dashboards.

---

## 2.3 Normalization schema (canonical contract)

Define canonical schema package (example entities):

1. `MarketBar`
   - `venue`, `symbol`, `instrument_id`, `bar_type`, `ts_event`, `ts_init`
   - `open`, `high`, `low`, `close`, `volume`

2. `MarketTick`
   - `venue`, `symbol`, `instrument_id`, `ts_event`, `price`, `size`, `side?`

3. `SignalEvent`
   - `strategy_id`, `instrument_id`, `ts_event`, `signal_type`, `strength`, `meta`

4. `OrderIntent`
   - `strategy_id`, `instrument_id`, `side`, `qty`, `order_type`, `risk_ref`, `ts_event`

5. `ExecutionReport`
   - broker-native fields + normalized status lifecycle (`NEW/ACK/PARTIAL/FILLED/CANCELED/REJECTED`)

All timestamps must be UTC, serialized in RFC3339 + ns where applicable.

---

## 2.4 Event bus / queue

### Phase 1 recommendation
- Start with in-process async queue for Nautilus runtime actors.
- Optional bridge for external queue (Redis Streams/NATS/Kafka) via publisher adapters.

### Event channels (logical)
- `market.raw.*`
- `market.normalized.*`
- `signal.*`
- `risk.*`
- `order.intent.*`
- `order.execution.*`
- `ops.metrics.*`

### Reliability baseline
- At-least-once delivery for externalized events.
- Idempotent consumers using event key (`instrument_id + ts_event + source_seq`).

---

## 2.5 Backtest path vs Live trading path

## Backtest path
1. Historical ingestion -> normalized dataset.
2. Feed adapter to Nautilus backtest engine.
3. Strategy + risk + execution simulator.
4. Persist metrics + trades + equity curve (same schema as live outputs).

## Live path
1. Live adapter (Binance WS / Shioaji callback).
2. Normalize -> event bus.
3. Nautilus strategy actor.
4. Risk engine -> order intent.
5. Execution adapter (Binance/Shioaji).
6. Execution reports -> state reconciliation + observability.

**Parity rule:** strategy and risk modules are shared; only data/execution adapters differ.

---

## 2.6 Risk / execution / order routing

### Risk layer (pre-trade)
- Max position per instrument/venue.
- Daily loss cap and strategy-level kill switch.
- Order frequency throttling.
- Session/market-hours guard.

### Execution routing
- `OrderRouter` chooses adapter based on `venue + instrument`.
- Pluggable policies:
  - market/limit preference
  - passive/aggressive mode
  - fallback cancellation/retry

### Post-trade controls
- Fill reconciliation.
- Slippage tracking vs reference price.
- Reject reason classification dashboard.

---

## 2.7 Monitoring metrics and logging

### Metrics (minimum)
- Ingestion lag (`exchange_ts -> ingest_ts`).
- Event throughput per channel.
- Strategy decision latency.
- Order ack latency / fill latency.
- Reject ratio, cancel ratio, slippage bps.
- PnL, drawdown, exposure, utilization.

### Logging
- Structured JSON logs with trace fields:
  - `run_id`, `strategy_id`, `instrument_id`, `event_id`, `correlation_id`
- Levels:
  - INFO for normal lifecycle
  - WARN for degraded but recoverable states
  - ERROR for failed critical path

### Observability stack
- Keep InfluxDB + dashboard path (already partially implemented).
- Add metric naming conventions aligned with event contract.

---

## 3) Nautilus-First Minimal Viable Directory Structure

```text
nautilus_lab/
  pyproject.toml
  docs/
    trading_framework_blueprint.md
    data_contracts.md
    runbooks/
      live_ops.md
      incident_recovery.md
  config/
    venues/
      binance.yaml
      shioaji.yaml
    strategies/
      trendpullback_v1_0_0_h1_l.yaml
  nautilus_lab/
    domain/
      models.py              # canonical events/entities
      enums.py
      ids.py
    adapters/
      binance/
        historical.py
        live_ws.py
        execution.py
      shioaji/
        historical.py
        live_bridge.py
        execution.py
      normalization/
        mapper.py
    bus/
      inproc_bus.py
      external_bridge.py
    engines/
      backtest_runner.py
      live_runner.py
    strategy/
      trendpullback/
        signals.py
        params.py
    risk/
      pre_trade.py
      position_limits.py
      kill_switch.py
    routing/
      order_router.py
    observability/
      metrics.py
      logging.py
      influx_sink.py
    storage/
      raw_store.py
      parquet_store.py
      state_store.py
  scripts/
    migrate/
      legacy_scan_report.py
    run/
      run_backtest.py
      run_live.py
```

Responsibility split:
- `domain`: pure business contracts.
- `adapters`: broker/exchange specifics only.
- `engines`: orchestration for backtest/live.
- `strategy`: signal generation without I/O.
- `risk/routing`: order governance and dispatch.
- `observability/storage`: non-trading side effects.

---

## 4) Executable Backlog (P0/P1/P2) with Acceptance Criteria

## P0 (must-do for safe convergence)

1. **Create canonical data contract package**
   - Deliverable: `nautilus_lab/domain/*` with typed entities and validators.
   - Acceptance:
     - Unit tests for timestamp/field validation.
     - Binance + Shioaji sample payloads can map into canonical schema.

2. **Wrap legacy data sources with adapter interfaces**
   - Deliverable: `adapters/binance/historical.py`, `adapters/shioaji/historical.py`.
   - Acceptance:
     - Existing fetch behavior preserved.
     - No direct strategy module imports from legacy scripts.

3. **Isolate strategy logic from scripts**
   - Deliverable: move signal rules to `strategy/trendpullback/signals.py`.
   - Acceptance:
     - One shared signal function used by both backtest and monitor path.
     - Regression check: same input data yields same signal points.

4. **Unify structured logging and event IDs**
   - Deliverable: common logger helper + event key convention.
   - Acceptance:
     - New logs include `run_id`, `strategy_id`, `instrument_id`, `event_id`.
     - JSONL remains backward compatible.

## P1 (operational maturity)

1. **Implement Shioaji live bridge with reconnect/resubscribe**
   - Acceptance:
     - Simulated disconnect test auto-recovers within configured timeout.
     - Re-subscription state restored.

2. **Introduce in-process event bus layer**
   - Acceptance:
     - Data adapter and strategy are decoupled via bus interfaces.
     - At least one replay test from stored raw events.

3. **Build backtest/live parity test suite**
   - Acceptance:
     - Same historical window through backtest and live-replay path gives equivalent signal sequence.

4. **Order router skeleton + risk pre-checks**
   - Acceptance:
     - Blocking rules (position cap/daily loss) enforced before adapter execution call.

## P2 (scale and resilience)

1. **External queue bridge (Redis Streams or NATS)**
   - Acceptance:
     - Consumer restart does not lose acknowledged progress.

2. **Contract rollover and instrument lifecycle manager**
   - Acceptance:
     - Front-month switch test passes without symbol ambiguity.

3. **Advanced observability SLO dashboard**
   - Acceptance:
     - P95 decision latency and fill latency visible by venue/strategy.

4. **Failure-injection runbook automation**
   - Acceptance:
     - Documented and scripted chaos checks for API timeout, malformed ticks, partial fills.

---

## 5) Migration Strategy (Non-Breaking)

1. Keep `scripts/*` runnable during Phase 1.
2. Introduce new modules under `nautilus_lab/` and call them from wrappers.
3. Run dual-path validation (legacy vs adapter output diff).
4. Switch production entrypoints only after parity checks pass.

---

## Appendix A: Immediate Reuse Mapping

- Reuse now:
  - `scripts/etl/core_data_sources.py` (as adapter internals)
  - `scripts/etl/cache_store.py`
  - `scripts/monitor/utils_*.py`
  - `nautilus_lab/nautilus_lab/influx_actor.py`

- Freeze (reference only):
  - `archive/20260303/legacy/*.py`

- Refactor first:
  - `scripts/monitor/monitor_run.py`
  - `scripts/monitor/monitor_core.py`

---

## Appendix B: Definition of Done for Phase 1

Phase 1 is complete when:
1. Canonical schema exists and is tested.
2. Binance/Shioaji adapters emit normalized events.
3. Strategy logic is shared (backtest + monitor/live replay).
4. Basic risk gate + routing skeleton exists.
5. Operational docs/runbook exist and are executable by another engineer.
