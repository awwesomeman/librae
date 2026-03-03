# Trigger Map

## 回測分析觸發
關鍵語句：
- 幫我回測這個策略
- 分析單資產策略回測結果
- 給我回測報告

執行規則：
1. 預設先輸出 brief（`templates/backtest_report_brief.md`）
2. 使用者要求詳細時再輸出 full（`templates/backtest_report_full.md`）
3. 必含欄位：交易次數/勝率/平均報酬率（每筆）/PF/權益倍數/年化報酬/夏普/MDD

## 穩健性測試觸發
關鍵語句：
- 幫我做穩健性測試
- 跑 walk-forward
- 成本壓測/穩定區

執行規則：
1. 使用 robust 模板（`templates/robustness_report.md`）
2. 要包含：WF 每窗、穩定區、成本壓測

## 待辦清單觸發
關鍵語句：
- 看待辦清單
- 待辦進度

執行規則：
1. 固定輸出 完成/進行中/待執行
2. 每項必有簡單描述

## 監控策略觸發
關鍵語句：
- 幫我監控這個策略
- 監控有在跑嗎

執行規則：
1. 回報 setup/trigger 排程
2. 回報 state 與 log 路徑
3. 回報去重機制（signal_key）
