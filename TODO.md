# TODO

> 時間一律 UTC；未完成的 `完成時間` 填 `-`。

## 進行中

- [ ] 建立「單一資產策略研究規劃清單」（可跨標的套用、參數再優化）
  - 分派時間：2026-03-02 14:05 UTC
  - 完成時間：-
  - 狀態描述：已完成來源池初版（`research/source_pool.md`），下一步整理策略候選與驗證路線。

- [ ] MultiFactorScore_v1.0-H1-L-TSI 穩健化
  - 分派時間：2026-03-02 10:54 UTC
  - 完成時間：-
  - 狀態描述：已完成第一輪 WF + 穩定區 + 成本壓測（`scripts/tsi_multifactorscore_robust.py`）；待市場狀態拆解與報告定稿。

- [ ] MultiFactorScore_v1.0-H1-LS-BTCF 穩健化
  - 分派時間：2026-03-02 13:50 UTC
  - 完成時間：-
  - 狀態描述：已跑出初版 OOS，待做 walk-forward、成本壓測、穩定區測試。

- [ ] 策略命名規則升級（回填舊文檔與腳本引用）
  - 分派時間：2026-03-02 14:06 UTC
  - 完成時間：-
  - 狀態描述：主命名規則已定，待把剩餘歷史引用與報告標籤補齊。

- [ ] 研究框架模組化（回測共用引擎）
  - 分派時間：2026-03-02 17:15 UTC
  - 完成時間：-
  - 狀態描述：`run_backtest.py`、`run_walkforward.py`、`run_stability.py`、`render_report.py` 已完成；下一步把主策略腳本全面接入共用框架。 

- [ ] 全域模板治理（single source + schema 檢核）
  - 分派時間：2026-03-02 17:30 UTC
  - 完成時間：-
  - 狀態描述：已確認方向；待新增 `templates/robustness_report.md`、`templates/report_schema.json`，並完成 latest/history 一鍵輸出流程。

## 待執行
- [ ] 連續期貨換月資料調整研究（TSI ETL v2）
  - 分派時間：2026-03-02 16:03 UTC
  - 完成時間：-
  - 狀態描述：尚未啟動；將檢查 MXFR1 是否未調整並建立 back-adjust 流程。

- [ ] TrendPullback_v1.0-H1-L-BTC 穩健化
  - 分派時間：2026-03-02 16:05 UTC
  - 完成時間：-
  - 狀態描述：尚未啟動；預計執行成本壓測（8/12/16 bps）與 WF。

- [ ] MomentumVolTarget_v1.0-H1-LS-TSI（TSMOM + Vol Targeting）
  - 分派時間：2026-03-02 14:05 UTC
  - 完成時間：-
  - 狀態描述：尚未開始，排在 RegimeSwitch 前。

- [ ] RegimeSwitch_v1.0-H1-LS-TSI（Trend/Range Regime）
  - 分派時間：2026-03-02 14:05 UTC
  - 完成時間：-
  - 狀態描述：尚未開始，待單一資產研究規劃清單定稿後啟動。

- [ ] RegimeProbModel_v1.0-H1-LS-TSI（Logistic/XGBoost，可解釋）
  - 分派時間：2026-03-02 14:05 UTC
  - 完成時間：-
  - 狀態描述：尚未開始，作為 RegimeSwitch 第二階段。

## 完成
- [x] 監控腳本模組化（monitor core/profile/runner）
  - 分派時間：2026-03-02 17:42 UTC
  - 完成時間：2026-03-03 00:29 UTC
  - 狀態描述：BTC/TSI 皆已切到統一 `monitor_run.py + monitor_profiles/*.json`，cron 全部切換完成；TSI 已移除 legacy adapter 依賴，改為 runner 原生邏輯。

- [x] 監控日誌標準化（統一 log schema for signal tracking）
  - 分派時間：2026-03-02 17:54 UTC
  - 完成時間：2026-03-03 00:29 UTC
  - 狀態描述：`data/monitor/signals.jsonl` 已統一欄位並套用 BTC/TSI（含 setup/trigger/skip/duplicate/signal_emitted 狀態），可直接追蹤訊號生命週期。

- [x] TrendPullback_v1.0-H1-L-BTC 初版回測（固定規則、無測試集調參）
  - 分派時間：2026-03-02 16:05 UTC
  - 完成時間：2026-03-02 16:08 UTC
  - 狀態描述：已完成初版 Train/OOS 回測並輸出績效。