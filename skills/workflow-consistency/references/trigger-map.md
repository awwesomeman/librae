# Trigger Map（Lean）

> 目的：快速判斷要套哪種輸出模板，避免重複解釋。

## 1) 回測分析
**常見語句**
- 幫我回測這個策略
- 分析單資產策略回測結果
- 給我回測報告

**執行規則**
1. 預設輸出 brief：`templates/backtest_report_brief.md`
2. 使用者要求再輸出 full：`templates/backtest_report_full.md`
3. 必含欄位：
   - 樣本期間（Train / Validation / OOS）
   - 成本設定（含 `round_trip_cost=...`）
   - 交易次數、勝率、平均報酬率（每筆）、PF、權益倍數、年化報酬、夏普、MDD

## 2) 穩健性測試
**常見語句**
- 幫我做穩健性測試
- 跑 walk-forward
- 成本壓測 / 穩定區

**執行規則**
1. 使用：`templates/robustness_report.md`
2. 至少包含：WF 每窗結果、穩定區、成本壓測
3. 若有多策略，優先用欄位式比較（可延伸至 HTML）

## 3) 待辦清單
**常見語句**
- 看待辦清單
- 待辦進度
- 更新待辦

**執行規則**
1. 固定三段：完成 / 進行中 / 待執行
2. 每項需有短描述（目前狀態 + 下一步）
3. 已完成項要即時回填，不留殘值

## 4) 監控策略
**常見語句**
- 幫我監控這個策略
- 監控有在跑嗎

**執行規則**
1. 回報 setup / trigger（排程與條件）
2. 回報 state + log 路徑
3. 回報去重機制（signal_key）

## 5) 命名提醒（跨情境）
- 正式命名使用：`[StrategyName]_v[L].[F].[P]-[TF]-[Side]-[Asset]`
- 實驗後綴只用在臨時產物，不進正式檔名/正式報告
