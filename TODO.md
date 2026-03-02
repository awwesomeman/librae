# TODO

## Active
- [ ] 建立「單一資產策略研究規劃清單」（可跨標的套用、參數再優化）
  - 狀態：進行中
  - 說明：整理研究流程、驗證標準、落地條件，作為策略研發主清單。
  - 進度：已建立 A.研究來源池初版 `research/source_pool.md`（含國內外論壇、論文庫、官方資料、來源評級規則）。

- [ ] 多因子策略穩健化（TSI）
  - 狀態：進行中
  - 說明：Walk-forward、參數穩定區、成本壓測（2/3/4點）、市場狀態拆解。
  - 進度：已完成第一輪 WF + 穩定區 + 成本壓測腳本與結果（`scripts/tsi_multifactorscore_robust.py`）。待做市場狀態拆解與報告定稿。

- [ ] MultiFactorScore_v1.0-H1-LS-BTCF 穩健化
  - 狀態：進行中
  - 說明：Walk-forward、成本壓測、參數穩定區，確認 OOS 穩定性。

- [ ] 策略命名規則升級（標的放最後）
  - 狀態：待開始
  - 說明：改成「策略名稱+版本+頻率+標的」格式，並回填舊策略名稱。

## Queue
- [ ] 動能＋波動調整策略（TSMOM + Vol Targeting）
- [ ] 狀態切換模型（Trend/Range Regime）
- [ ] 輕量 ML 訊號（Logistic/XGBoost，可解釋）
