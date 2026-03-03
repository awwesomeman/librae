# 策略命名規則回填報告（KEEP 檔案）

日期：2026-03-03 UTC

## 規則
`[StrategyName]_v[Major].[Minor]-[TF]-[Side]-[Asset]`

## 檢查範圍（KEEP / Active）
- scripts/strategy_multifactorscore_v1_1_0_h1_l_mxfr1.py（原：multifactorscore_v1_1_0_h1_l_mxfr1.py → wrapper）
- scripts/strategy_trendpullback_v1_1_0_h1_l_btc.py（原：trendpullback_v1_1_0_h1_l_btc.py → wrapper）
- scripts/strategy_multifactorscore_v1_0_0_h1_ls_btcf.py（原：strategy_multifactorscore_v1_0_0_h1_ls_btcf.py → wrapper）
- scripts/monitor_profiles/btc_trendpullback_v1_0_0_h1_l.json
- scripts/monitor_profiles/mxfr1_trendpullback_v1_0_0_h1_l.json
- TODO.md

## 回填結果
- `MultiFactorScore_v1.1.0-H1-L-MXFR1`：符合
- `TrendPullback_v1.0.0-H1-L-BTC`：符合
- `TrendPullback_v1.1.0-H1-L-BTC`：符合（候選版，保留未採用語境）
- `MultiFactorScore_v1.0.0-H1-LS-BTCF`：符合
- `TrendPullback_v1.0.0-H1-L-MXFR1`：符合

## 補充
- 已完成 KEEP 檔案命名引用回填與一致性檢查。
- 歷史敘述檔（memory 與 archive）保留原始脈絡，不強制覆寫，以免破壞歷史可追溯性。
- 2026-03-03：三支主策略腳本已重新命名為 `strategy_` 前綴 snake_case 格式；原路徑保留向後相容 wrapper（import + call main()）。
