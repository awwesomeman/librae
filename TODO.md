# TODO

> 時間一律 UTC；未完成的 `完成時間` 填 `-`。

## 進行中

- [ ] 建立「單一資產策略研究規劃清單」（可跨標的套用、參數再優化）
  - 分派時間：2026-03-02 14:05 UTC
  - 完成時間：-
  - 狀態描述：已完成來源池初版（`research/source_pool.md`），下一步整理策略候選與驗證路線。

- [ ] MultiFactorScore_v1.0.0-H1-L-MXFR1 穩健化
  - 分派時間：2026-03-02 10:54 UTC
  - 完成時間：-
  - 狀態描述：已完成第一輪 WF + 穩定區 + 成本壓測；待市場狀態拆解與報告定稿。

- [ ] MultiFactorScore_v1.0.0-H1-LS-BTCF 穩健化
  - 分派時間：2026-03-02 13:50 UTC
  - 完成時間：-
  - 狀態描述：已跑出初版 OOS，待做 walk-forward、成本壓測、穩定區測試。



## 待執行



- [ ] 搜尋開源參數最佳化模組（參數組合探索）
  - 分派時間：2026-03-03 06:31 UTC
  - 完成時間：-
  - 狀態描述：盤點可用開源方案（Optuna/Hyperopt/Nevergrad/Skopt）並評估與現有共用回測框架整合方式。

- [ ] 低頻版 TrendPullback 研究（D1 決策 + H1 觸發，MXFR1）
  - 分派時間：2026-03-03 08:32 UTC
  - 完成時間：-
  - 狀態描述：策略主邏輯維持 TrendPullback，不改核心風控結構；決策週期改為日頻（D1），進場觸發使用 H1。版本命名建議採 `TrendPullback_v2.0.0-D1-L-MXFR1`（週期/決策框架變更屬 Major）。

- [ ] 穩健性測試報告 HTML 化
  - 分派時間：2026-03-03 08:32 UTC
  - 完成時間：-
  - 狀態描述：將 Walk-Forward / 成本壓測 / 穩定區等穩健性輸出整合為可視化 HTML 報告，格式與回測報告一致，支援多策略欄位比較與手機版瀏覽。


- [ ] Brave Web Search 升級規劃（稍晚提醒）
  - 分派時間：2026-03-03 00:53 UTC
  - 完成時間：-
  - 狀態描述：先記錄為今日稍晚提醒項，屆時協助你檢查 API key 與升級路線。

- [ ] 連續期貨換月資料調整研究（MXFR1 ETL v2）
  - 分派時間：2026-03-02 16:03 UTC
  - 完成時間：-
  - 狀態描述：尚未啟動；將檢查 MXFR1 是否未調整並建立 back-adjust 流程。

- [ ] TrendPullback_v1.0.0-H1-L-BTC 穩健化
  - 分派時間：2026-03-02 16:05 UTC
  - 完成時間：-
  - 狀態描述：尚未啟動；預計執行成本壓測（8/12/16 bps）與 WF。

- [ ] MomentumVolTarget_v1.0.0-H1-LS-TSI（TSMOM + Vol Targeting）
  - 分派時間：2026-03-02 14:05 UTC
  - 完成時間：-
  - 狀態描述：尚未開始，排在 RegimeSwitch 前。

- [ ] RegimeSwitch_v1.0.0-H1-LS-TSI（Trend/Range Regime）
  - 分派時間：2026-03-02 14:05 UTC
  - 完成時間：-
  - 狀態描述：尚未開始，待單一資產研究規劃清單定稿後啟動。

- [ ] RegimeProbModel_v1.0.0-H1-LS-TSI（Logistic/XGBoost，可解釋）
  - 分派時間：2026-03-02 14:05 UTC
  - 完成時間：-
  - 狀態描述：尚未開始，作為 RegimeSwitch 第二階段。

## 完成
- [x] Binance API 防限流 + ETL 快取層（模組化）
  - 分派時間：2026-03-03 07:06 UTC
  - 完成時間：2026-03-03 07:30 UTC
  - 狀態描述：`scripts/etl/core_data_sources.py` 已完成 backoff+jitter、Retry-After、pacing、chunking；新增 `scripts/etl/cache_store.py`（TTL + key 標準化 + 原子寫入），並接入 spot/futures 抓取。

- [x] v1.0.0 基線策略檔名與資產命名同步（TSI → MXFR1）
  - 分派時間：2026-03-03 08:10 UTC
  - 完成時間：2026-03-03 08:19 UTC
  - 狀態描述：已將 `strategy_*_v1_0_0_h1_l_mxfr1.py` 更名為 `strategy_*_v1_0_0_h1_l_mxfr1.py`，並更新測試引用與輸出 strategy 名稱。

- [x] 四策略基線重置（v1.0.0 重新選參與回測）
  - 分派時間：2026-03-03 06:31 UTC
  - 完成時間：2026-03-03 08:21 UTC
  - 狀態描述：已完成四策略基線重置與重跑，目標為 `TrendPullback_v1.0.0-H1-L-MXFR1`、`TrendPullback_v1.0.0-H1-L-BTCF`、`MultiFactorScore_v1.0.0-H1-L-MXFR1`、`MultiFactorScore_v1.0.0-H1-L-BTCF`；流程採 Train+Validation 選參後 OOS 一次驗證，並已輸出最新績效報告（含 sample_periods 與統一成本格式）。

- [x] 報告 schema 集中管理（schema_builder 消除策略腳本重複組裝）
  - 分派時間：2026-03-03 UTC
  - 完成時間：2026-03-03 UTC
  - 狀態描述：新增 `scripts/reporting/schema_builder.py`（build_sample_periods / build_cost_settings / build_strategy_output / validate_against_report_schema）；四支 v1.0.0 策略腳本已改用集中建構，移除手動 dict 組裝重複；13 項單元測試全數通過。

- [x] 績效模板欄位補強（樣本期間 + 交易成本）
  - 分派時間：2026-03-03 07:12 UTC
  - 完成時間：2026-03-03 07:12 UTC
  - 狀態描述：已在精簡版/詳細版/穩健性模板補上各段樣本期間與交易成本設定欄位，並同步更新 workflow skill 的輸出欄位規範。
- [x] 監控腳本模組化（monitor core/profile/runner）
  - 分派時間：2026-03-02 17:42 UTC
  - 完成時間：2026-03-03 00:29 UTC
  - 狀態描述：BTC/TSI 皆已切到統一 `scripts/monitor/monitor_run.py + scripts/monitor/monitor_profiles/*.json`，cron 全部切換完成；TSI 已移除 legacy adapter 依賴，改為 runner 原生邏輯。

- [x] 監控日誌標準化（統一 log schema for signal tracking）
  - 分派時間：2026-03-02 17:54 UTC
  - 完成時間：2026-03-03 00:29 UTC
  - 狀態描述：`data/monitor/signals.jsonl` 已統一欄位並套用 BTC/TSI（含 setup/trigger/skip/duplicate/signal_emitted 狀態），可直接追蹤訊號生命週期。

- [x] 研究框架模組化（回測共用引擎）
  - 分派時間：2026-03-02 17:15 UTC
  - 完成時間：2026-03-03 03:09 UTC
  - 狀態描述：主策略腳本 `scripts/strategies/strategy_multifactorscore_v1_1_0_h1_l_mxfr1.py`、`scripts/strategies/strategy_trendpullback_v1_1_0_h1_l_btc.py`、`scripts/strategies/strategy_multifactorscore_v1_0_0_h1_ls_btcf.py` 已接入共用引擎（run_backtest/run_walkforward/run_stability）；向後相容 wrapper 已於 2026-03-03 移除；已通過 py_compile 與單元測試。

- [x] ETL 與回測模組解耦重構（多資料源一致化）
  - 分派時間：2026-03-03 01:49 UTC
  - 完成時間：2026-03-03 03:29 UTC
  - 狀態描述：已新增 `scripts/etl/core_data_sources.py`（Binance Spot/Futures + Shioaji adapter）與 `scripts/etl/core_features.py`（統一特徵工程）；並完成三支主策略腳本（`scripts/strategies/strategy_multifactorscore_v1_1_0_h1_l_mxfr1.py`、`scripts/strategies/strategy_trendpullback_v1_1_0_h1_l_btc.py`、`scripts/strategies/strategy_multifactorscore_v1_0_0_h1_ls_btcf.py`）接入與測試（9/9 pass）。

- [x] 應用場景專用 Skills 設計
  - 分派時間：2026-03-03 00:53 UTC
  - 完成時間：2026-03-03 03:25 UTC
  - 狀態描述：`workflow-consistency` 與 `artifact-lifecycle-manager` 已完成骨架+規則強化，並補齊 `references/trigger-map.md`、`references/cleanup-flow.md`。

- [x] 策略命名規則升級（回填舊文檔與腳本引用）
  - 分派時間：2026-03-02 14:06 UTC
  - 完成時間：2026-03-03 03:23 UTC
  - 狀態描述：已完成 KEEP 檔案回填與一致性檢查；結果記錄於 `research/naming_backfill_report_2026-03-03.md`。

- [x] 全域模板治理（single source + schema 檢核）
  - 分派時間：2026-03-02 17:30 UTC
  - 完成時間：2026-03-03 02:58 UTC
  - 狀態描述：已新增 `templates/robustness_report.md`、`templates/report_schema.json`，並完成 `scripts/reporting/save_reports.py`（latest/history 一鍵存檔）。

- [x] 舊版資產清理審查（回測/監控/報告）
  - 分派時間：2026-03-03 00:53 UTC
  - 完成時間：2026-03-03 03:21 UTC
  - 狀態描述：已完成審查並依確認執行清理：archive 至 `archive/20260303/`，低價值產物移至 `archive/20260303/trash/`（可恢復）。

- [x] TrendPullback_v1.0.0-H1-L-BTC 初版回測（固定規則、無測試集調參）
  - 分派時間：2026-03-02 16:05 UTC
  - 完成時間：2026-03-02 16:08 UTC
  - 狀態描述：已完成初版 Train/OOS 回測並輸出績效。