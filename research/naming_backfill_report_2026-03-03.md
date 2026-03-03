# 策略命名規則回填報告（KEEP 檔案）

日期：2026-03-03 UTC

## 規則
`[StrategyName]_v[Major].[Minor]-[TF]-[Side]-[Asset]`

## 檢查範圍（KEEP / Active）
- scripts/multifactorscore_v1_1_h1_l_tsi.py
- scripts/trendpullback_v1_1_h1_l_btc_robust.py
- scripts/btcf_m1_0_m1.py
- scripts/monitor_profiles/btc_trendpullback_v1_0_h1_l.json
- scripts/monitor_profiles/tsi_trendpullback_v1_0_h1_l.json
- TODO.md

## 回填結果
- `MultiFactorScore_v1.1-H1-L-TSI`：符合
- `TrendPullback_v1.0-H1-L-BTC`：符合
- `TrendPullback_v1.1-H1-L-BTC`：符合（候選版，保留未採用語境）
- `MultiFactorScore_v1.0-H1-LS-BTCF`：符合
- `TrendPullback_v1.0-H1-L-TSI`：符合

## 補充
- 已完成 KEEP 檔案命名引用回填與一致性檢查。
- 歷史敘述檔（memory 與 archive）保留原始脈絡，不強制覆寫，以免破壞歷史可追溯性。
