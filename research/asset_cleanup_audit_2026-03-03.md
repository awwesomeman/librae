# 舊版資產清理審查（Audit）

日期：2026-03-03 UTC

## 摘要
- keep: 15
- archive: 8
- delete: 9

> 目前僅審查，不執行刪除。等你確認後再動作。

## KEEP（仍在使用 / 近期高機率使用）
- scripts/run_backtest.py
- scripts/run_walkforward.py
- scripts/run_stability.py
- scripts/render_report.py
- scripts/save_reports.py
- scripts/monitor_core.py
- scripts/monitor_run.py
- scripts/monitor_profiles/btc_trendpullback_v1_0_0_h1_l.json
- scripts/monitor_profiles/mxfr1_trendpullback_v1_0_0_h1_l.json
- templates/backtest_report_brief.md
- templates/backtest_report_full.md
- templates/robustness_report.md
- templates/report_schema.json
- data/monitor/signals.jsonl
- TODO.md

## ARCHIVE（保留追溯價值，但不建議日常直接使用）
- scripts/mxf_setup_scan.py  # 已由 monitor_run 取代
- scripts/mxf_trigger_watch.py  # 已由 monitor_run 取代
- scripts/btc_trendpullback_v1_0_monitor.py  # 已由 monitor_run 取代
- scripts/tsi_multifactorscore_robust.py  # 已有共用框架版本
- scripts/trendpullback_v1_0_0_h1_l_btc.py  # 舊版單體回測
- data/binance/btc_trendpullback_v1_0_monitor.log  # 舊監控日誌
- data/shioaji/mxf_setup_scan.log  # 舊監控日誌
- data/shioaji/mxf_trigger_watch.log  # 舊監控日誌

## DELETE（低價值產物，建議清除）
- scripts/__pycache__/  # 編譯快取
- data/shioaji/MXF_v2_signals_7d.png  # 可再生圖檔
- data/shioaji/MXF_v2_signals_60d.png  # 可再生圖檔
- data/shioaji/MXF_v2_last_trade_zoom.png  # 可再生圖檔
- data/shioaji/MXF_backtest_summary.json  # 舊版輸出
- data/shioaji/MXF_backtest_trades.csv  # 舊版輸出
- data/shioaji/MXF_60m.csv  # 中間產物
- data/shioaji/MXF_1d.csv  # 中間產物
- data/shioaji/MXF_v2_plot_meta.json  # 舊版中間產物

## 執行記錄（已執行）
- 執行時間：2026-03-03 03:21 UTC
- 已將 ARCHIVE 項目移至 `archive/20260303/`。
- 已將 DELETE 項目移至 `archive/20260303/trash/`（可恢復）。
- 待後續觀察期滿再決定是否永久刪除。
