# 2026-03-26 — Dashboard 資料範圍定義

> 狀態：accepted

## 決策

- **Grafana**：只放即時監控資料（sim-live / live runner 持續寫入）
- **Streamlit**：只放回測分析資料（一次性回測輸出）

## 理由

- 回測資料與即時資料時間軸不連續、信號來源不同，混合會造成混淆
- Grafana 強項是即時 time-series + 告警，Streamlit 強項是互動分析
- 兩邊資料都存在 InfluxDB（retention=infinite），但用不同 tag/run_id 區分

## 實作規範

- Grafana query 只查 `source=live` 或 `source=sim` 的資料
- 回測資料用 `source=backtest` tag，不顯示在 Grafana 主監控面板
