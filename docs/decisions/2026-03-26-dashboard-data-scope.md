# 2026-03-26 — Dashboard 資料範圍定義

> 狀態：superseded
> 取代者：04-02 db-schema-consolidation（資料寫入流程）、04-02 signal-monitor-dashboard-review（dashboard 設計）
> 注記：整份基於 InfluxDB tag 機制（source=live/backtest），與現行 TimescaleDB schema 不符。Grafana=監控 / Streamlit=分析 的分工原則仍有效，已被 03-06 涵蓋

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
