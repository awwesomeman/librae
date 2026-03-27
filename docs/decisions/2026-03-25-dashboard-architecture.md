# 002 — Dashboard Architecture（Streamlit vs Grafana 分工）

> 日期：2026-03-25
> 狀態：accepted

## 背景

原本考慮將 Grafana 退居二線、以 Streamlit 為唯一前端。經評估後決定雙系統各司其職。

## 決策

### Streamlit（研究分析 + 策略決策）
- 回測報告、參數比較
- 回測 vs 實盤疊圖（策略漂移偵測）
- Kill switch
- 使用場景：人在螢幕前

### Grafana（自動監控 + 告警）
- 不寫業務指標計算邏輯
- 只讀 InfluxDB 中已算好的指標（由 Python metrics.py 寫入）
- 負責：告警規則、系統健康、訊號延遲、心跳
- 使用場景：24/7 無人值守

### 指標邏輯 Single Truth
- 所有指標計算走 `quant_lab/backtest/metrics.py`
- 計算結果寫入 InfluxDB
- Grafana 只做讀 + 顯示 + 告警，不在 Flux 裡重算
- 避免雙重維護風險

## 理由
- 一人團隊不想盯盤 → 需要 Grafana 的自動告警
- Streamlit 沒有原生告警；自己造等於重複發明
- 指標邏輯統一在 Python 端 → 回測與實盤數據保證一致

## 替代方案（未採用）
- Streamlit 為唯一前端：告警能力不足，24/7 監控不實際
- Dash/Taipy 取代 Streamlit：現階段 H1 頻率 + 一人使用，投資報酬率不高
