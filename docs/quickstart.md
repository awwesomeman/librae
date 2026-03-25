# Quickstart: One-Command Execution

## Prerequisites

```bash
cd /home/jasonpan_subscribe/.openclaw/workspace/quant-strategy-lab
pip install -e nautilus_lab/
pip install -e ".[test]"
```

## 1. Run Backtest (TrendPullBack BTC H1)

```bash
python3 nautilus_lab/scripts/run_backtest_trendpullback_btc.py --save-dir output/backtest
```

Outputs:
- Console: trade count, return, Sharpe, MDD, all computed metrics
- `output/backtest/<run_id>.json` + `_equity_curve.csv`

## 2. Run Sim-Live Signal Scanner

```bash
# Single scan (no Telegram):
python3 nautilus_lab/scripts/run_sim_signal_trendpullback_btc.py --once

# Continuous loop with Telegram notifications:
export TELEGRAM_ENABLED=true
export TELEGRAM_BOT_TOKEN=your_token
export TELEGRAM_CHAT_ID=your_chat_id
python3 nautilus_lab/scripts/run_sim_signal_trendpullback_btc.py --telegram --interval 60
```

## 3. Launch Streamlit Dashboard

```bash
export INFLUX_URL=http://localhost:8086
export INFLUX_ORG=quant_research
export INFLUX_BUCKET=nautilus_signals
export INFLUX_TOKEN=your_token

streamlit run nautilus_lab/app/streamlit_performance.py
```

## 4. Seed Grafana Demo Data

```bash
# Dry-run (preview line protocol):
python3 scripts/grafana/seed_grafana_demo_data.py --dry-run

# Write to InfluxDB:
python3 scripts/grafana/seed_grafana_demo_data.py \
    --url http://localhost:8086 \
    --token YOUR_TOKEN \
    --org nautilus \
    --bucket nautilus
```

## 5. Run Tests

```bash
python3 -m pytest tests/ -v
```

## Architecture Quick Reference

```
Strategy Core (trendpullback_btc.py)
    |
    +-- generate_signals()  --> sim-live runner
    +-- backtest()          --> backtest runner
            |
            +-- metrics_dict_to_backtest_output()  --> BacktestOutput
                    |
                    +-- compute_all()        --> 11 metrics (Sharpe, Sortino, Calmar, ...)
                    +-- save_backtest_output()  --> JSON + CSV
                    +-- points_from_backtest()  --> InfluxDB --> Grafana
                    +-- Streamlit dashboard   --> reads InfluxDB or JSON
```

## Telegram Feature Flag

Telegram notifications are off by default. Control via:
- Environment: `TELEGRAM_ENABLED=true|false`
- Constructor: `TelegramAdapter(enabled=True, bot_token="...", chat_id="...")`
- CLI flag: `--telegram` on the sim runner
