#!/bin/bash
# Start signal monitor scheduler in background (nohup)
# Edit INFLUX_TOKEN before first use!

cd /home/jasonpan_subscribe/.openclaw/workspace/quant-strategy-lab

export INFLUX_TOKEN=change_me_super_secret_token
export INFLUX_ORG=quant_research
export INFLUX_BUCKET=nautilus_signals
# export MONITOR_SYMBOL=BTC/USDT    # default
# export MONITOR_TIMEFRAME=1h       # default
# export CCXT_API_KEY=              # optional
# export CCXT_API_SECRET=           # optional

nohup .venv/bin/python scripts/monitor/scheduler.py > /tmp/signal_monitor.log 2>&1 &
echo "PID: $!"
