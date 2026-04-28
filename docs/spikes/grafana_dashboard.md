# Grafana Dashboard Guide

## Dashboard: Quant Strategy Monitor

**File:** `nautilus_lab/grafana/dashboards/signal_monitoring_dashboard_v0_mvp.json`

### Overview

A comprehensive Grafana dashboard for monitoring quantitative trading strategies in real-time. Organized into 6 sections:

| Section | Panels | Purpose |
|---------|--------|---------|
| Strategy Health | Heartbeat, Signals (1h), Error Rate, Data Latency, Avg Strength, Win Rate | At-a-glance system health |
| Equity & PnL | Equity Curve, PnL per period | Portfolio performance tracking |
| Drawdown | Drawdown (%), Max Drawdown gauge, Drawdown Duration | Risk monitoring |
| Signal Quality | Signal Strength Timeline, Signal Type Distribution, Win Rate (Rolling 7d), Signal Count | Signal analysis |
| Data Latency & System | Latency (p50/p95), Error Rate over time | Infrastructure health |
| Signal Detail | Recent Signals table | Raw signal inspection |

### Prerequisites

- Grafana 9.x+ with InfluxDB (Flux) datasource configured
- InfluxDB 2.x with a bucket named `nautilus` (configurable)

### Import Dashboard

1. Open Grafana > **Dashboards** > **Import**
2. Click **Upload JSON file** and select:
   ```
   nautilus_lab/grafana/dashboards/signal_monitoring_dashboard_v0_mvp.json
   ```
3. After import, update the hidden variable `influxdb_uid`:
   - Go to **Dashboard Settings** > **Variables**
   - Find `influxdb_uid` and replace `replace_with_influxdb_uid` with your InfluxDB datasource UID
   - (Find UID in **Configuration** > **Data sources** > select your InfluxDB > copy UID from URL)
4. Save the dashboard

### Seed Demo Data (Optional)

To populate the dashboard with realistic demo data:

```bash
# Dry-run (preview line protocol output):
python3 scripts/grafana/seed_grafana_demo_data.py --dry-run

# Write to InfluxDB:
python3 scripts/grafana/seed_grafana_demo_data.py \
    --url http://localhost:8086 \
    --token YOUR_INFLUXDB_TOKEN \
    --org nautilus \
    --bucket nautilus
```

Requires `influxdb-client`:
```bash
pip install influxdb-client
```

The seed script generates 7 days of data across 2 strategies (`momentum_btc`, `mean_revert_eth`), including signals, performance metrics, and health events.

### InfluxDB Measurements

The dashboard queries three measurements:

| Measurement | Fields | Description |
|-------------|--------|-------------|
| `strategy_signals` | `signal_strength`, `confidence`, `price`, `quantity` | Individual signal events |
| `strategy_performance` | `equity`, `pnl`, `drawdown_pct`, `drawdown_duration_hours`, `win_rate` | Portfolio/strategy metrics |
| `strategy_health` | `event_count`, `error_count`, `data_latency_ms` | System health telemetry |

### Dashboard Variables

Use the top-bar filters to narrow the view:

- **Strategy** — filter by strategy name (multi-select)
- **Asset** — filter by trading pair
- **Timeframe** — filter by candle timeframe (H1, H4, etc.)
- **Side** — buy, sell, or both
- **Signal Type** — entry, exit, scale_in, etc.
- **Run ID** — specific backtest/live run
- **Sample** — train or oos (out-of-sample)
- **Benchmark** — performance benchmark to compare against

### Threshold Reference

| Panel | Green | Orange | Red |
|-------|-------|--------|-----|
| Heartbeat | < 30 min | 30-120 min | > 120 min |
| Error Rate | < 1% | 1-5% | > 5% |
| Data Latency | < 500 ms | 500-2000 ms | > 2000 ms |
| Win Rate | > 50% | 40-50% | < 40% |
| Drawdown Duration | < 24h | 24-72h | > 72h |
