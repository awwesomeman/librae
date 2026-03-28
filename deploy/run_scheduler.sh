#!/bin/bash
# Start signal monitor scheduler in background (nohup)

cd /home/jasonpan_subscribe/.openclaw/workspace/quant-strategy-lab

# export MONITOR_SYMBOL=BTC/USDT    # default
# export MONITOR_TIMEFRAME=1h       # default
# export CCXT_API_KEY=              # optional
# export CCXT_API_SECRET=           # optional

nohup .venv/bin/python scripts/monitor/scheduler.py > /tmp/signal_monitor.log 2>&1 &
echo "PID: $!"
