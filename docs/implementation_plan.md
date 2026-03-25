# quant-strategy-lab Implementation Plan

> Updated: 2026-03-25
> Framework: **Lumibot-first** (backtest + live, single strategy class)
> Status: Phase 0 收尾中

---

## 1) Goal Alignment

### End goal
Signal subscription platform covering futures, crypto, pair trading, stock selection.

### Current phase goal
Single-asset, verifiable, monitorable MVP:
1. 用真實 Binance BTC 資料跑 Lumibot 策略
2. 計算主要績效指標（Python metrics.py 為唯一計算來源）
3. Streamlit 回測儀表板（研究分析 + 策略漂移偵測）
4. Grafana 監控儀表板（24/7 告警 + 系統健康）
5. 驗證策略正確性
6. 保留多資產擴展空間

---

## 2) Tech Stack

| Area | Tool | Rationale |
|------|------|-----------|
| Strategy framework | **Lumibot** | Unified backtest + live |
| Time-series storage | **InfluxDB 2.x** | Equity curve / signal / metrics |
| Dashboards | **Streamlit**（研究分析）+ **Grafana**（監控告警） | 見 decisions/2026-03-25-dashboard-architecture.md |
| Metrics engine | **metrics.py**（Python single truth） | Grafana 不重算，只讀已算好的值 |
| Alerts | Grafana Alerting → Telegram | 24/7 無人值守 |
| Scheduling | cron → Prefect（後續） | Start simple |
| Testing/CI | pytest + GitHub Actions | core / tw-live 分流 |
| Deployment | docker-compose | InfluxDB + Grafana |
| TW live trading | Shioaji（optional extra） | 獨立 `tw-live` 依賴組 |

---

## 3) Phase 進度追蹤

### Phase 0 — Foundation & E2E（目標：Day 30）

| 交付項目 | 狀態 |
|---------|------|
| docker-compose（InfluxDB + Grafana） | ✅ 完成 |
| Canonical schema | ✅ 完成 |
| 可擴展績效指標模組（metrics.py） | ✅ 完成 |
| TrendPullback BTC 策略核心 | ✅ 完成 |
| Backtest runner + Sim signal runner | ✅ 完成 |
| Lumibot PoC（100% 訊號一致率） | ✅ 完成 |
| Grafana MVP 監控儀表板 | ✅ 完成 |
| Streamlit 回測儀表板 | ✅ 完成（token/plotly 修復） |
| Telegram adapter（feature flag） | ✅ 完成 |
| Binance 真實資料抓取模組 | ✅ 完成 |
| **真實資料 → InfluxDB → 儀表板 e2e** | ⚠️ 進行中 |
| **Lumibot 正式整合（取代 PoC）** | ⚠️ 待執行 |
| CI 分流（core / tw-live） | ✅ 完成 |
| pyproject.toml 依賴分層 | ✅ 完成 |

### Phase 1 — Experiment tracking & comparison（目標：Day 60）

| 交付項目 | 狀態 |
|---------|------|
| MLflow server 整合 | 待執行 |
| ≥2 策略可比較 | 待執行 |
| Streamlit 策略漂移偵測（回測 vs 實盤疊圖） | 待執行 |
| 回歸基準測試上 CI | 待執行 |

### Phase 2 — Scheduling, notifications, API（目標：Day 90）

| 交付項目 | 狀態 |
|---------|------|
| 排程（cron/Prefect） | 待執行 |
| Telegram 訊號推播（正式版） | 待執行 |
| Grafana 告警規則（MDD、心跳） | 待執行 |
| FastAPI skeleton | 待執行 |

### Phase 3 — Multi-asset & subscription platform

| 交付項目 | 狀態 |
|---------|------|
| 多資產策略支援 | 待執行 |
| 使用者/訂閱（PostgreSQL + JWT） | 待執行 |
| 版本化策略發布 | 待執行 |

---

## 4) When to refactor

觸發任 2~3 條：
1. 策略 >10 且重複邏輯 >40%
2. 維護成本 >1.5x
3. 需要 tick/orderbook 回測
4. 訂閱者 >50 且 API 延遲成瓶頸
5. 資料源 >3 導致 adapter 不一致

---

## 5) Immediate Next Actions

1. 真實 Binance 資料回測結果寫入 InfluxDB → 儀表板顯示真實績效
2. Lumibot 正式整合為回測主入口
3. Streamlit 加入策略漂移偵測功能
4. 更新 Grafana seed 用真實資料

---

## 6) Principles

- 每個 Phase 都要可 demo、可驗收
- 不為了好看而提前重構
- 先可用再擴展；先正確再優化
- 指標計算 single truth（metrics.py）
- Lumibot 為策略執行唯一框架
