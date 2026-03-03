# Naming Backfill Targets

更新時間：2026-03-03 UTC

## 目標
將策略命名規則 `Strategy_vMajor.Minor-TF-Side-Asset` 回填到仍在使用或仍可能復用的檔案引用與報告標籤。

## 優先回填（Active）
- `scripts/multifactorscore_v1_1_0_h1_l_mxfr1.py`
- `scripts/tsi_multifactorscore_robust.py`
- `scripts/trendpullback_v1_1_0_h1_l_btc.py`
- `scripts/trendpullback_v1_0_0_h1_l_btc.py`
- `scripts/strategy_multifactorscore_v1_0_0_h1_ls_btcf.py`
- `scripts/monitor_profiles/btc_trendpullback_v1_0_0_h1_l.json`
- `scripts/monitor_profiles/mxfr1_trendpullback_v1_0_0_h1_l.json`
- `TODO.md`

## 次優先（歷史與敘述檔）
- `memory/2026-03-02.md`
- `memory/2026-03-03.md`
- `research/source_pool.md`（若有策略名稱提及）

## 備註
- 不先硬刪舊檔，避免誤砍；清理由「舊版資產清理審查」任務二階段處理。
- 回填時若遇到「候選未採用版本」，需保留版本脈絡但標註未採用，避免混淆。