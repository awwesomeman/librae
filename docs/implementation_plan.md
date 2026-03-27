# quant-strategy-lab Implementation Plan

> Updated: 2026-03-27
> Architecture: Signal Engine-first + MarketAdapter + TimescaleDB
> Status: Phase 1 進行中（策略研究工具 + 多策略擴展）

---

## 1) Goal Alignment

### End goal
Signal subscription platform for futures, crypto, pair trading, stock selection.

### Current phase goal
單一資產 MVP 可驗證、可監控，儀表板正確顯示回測與即時訊號。

---

## 2) Tech Stack（當前）

| Area | Tool | 說明 |
|------|------|------|
| Signal Engine | pure Python/Pandas + pandas_ta_classic | Single truth，pure function |
| 研究/參數掃描 | vectorbt（開源版） | 多 TF、向量化快速掃描 |
| 高保真回測 | 自建 bar-by-bar runner（含成本模型） | InstrumentConfig 驅動 |
| Market Config | `config/markets.yaml`（兩層架構） | 市場層（asset-class）+ 標的層 |
| 執行層 | CCXT / ib_insync / Shioaji | 依市場選工具 |
| Time-series DB | **TimescaleDB**（唯一資料源） | InfluxDB 已退役 |
| Dashboards | Streamlit（策略研究）+ Grafana（三板監控） | 分工明確 |
| Grafana 三板 | Backtest / Monitor / Live | Python generator，單一 source |
| Scheduler | APScheduler（每小時整點） | mode=sim 寫 TimescaleDB |
| Alerts | Grafana Alerting → Telegram | 待實作 |
| Testing | pytest 405/405 | core tests 全過 |
| Deployment | docker-compose（TimescaleDB + Grafana） | VPS: 35.194.150.232 |

### 設計原則
- **No-fallback**：所有資料源直連，連線失敗直接報錯
- **Single truth**：signal_engine 是唯一策略邏輯來源
- **TimescaleDB only**：所有讀寫走 `quant_lab/db/`，不依賴 InfluxDB

---

## 3) Phase 進度

### Phase 0 — Foundation ✅ 完成

| 項目 | 狀態 |
|------|------|
| signal_engine pure function（trendpullback） | ✅ |
| 自建回測引擎（成本模型、冪等重跑） | ✅ |
| Market Config 兩層架構（markets.yaml） | ✅ |
| CryptoAdapter + MarketHub | ✅ |
| TimescaleDB 完全取代 InfluxDB | ✅ |
| Grafana 三板（Python generator，2×2 版面） | ✅ |
| Streamlit（TimescaleDB 讀取，run_id 選擇） | ✅ |
| Scheduler（APScheduler，mode=sim） | ✅ |
| Look-ahead bias 自動化測試 | ✅ |
| QA 驗證（訊號/績效正確性，21/21 pass） | ✅ |

### Phase 1 — 策略研究工具（當前）

| 項目 | 狀態 |
|------|------|
| Streamlit 改版為 vectorbt 研究工具 | ⏳ |
| param_sweep_results 表（TimescaleDB） | ⏳ |
| 參數掃描結果互動式呈現 | ⏳ |
| ≥2 策略可比較（Backtest 板 run_id 對比） | ⏳ |
| Sharpe 改用 bar returns（quantstats 橋接） | ⏳ |
| InfluxDB container 退役 | ⏳ |
| Monitor 板真實 sim 資料驗證 | ⏳ |

### Phase 2 — Notifications & API

| 項目 | 狀態 |
|------|------|
| Grafana 告警規則 → Telegram | ⏳ |
| Telegram 訊號推播（正式版） | ⏳ |
| Grafana Public Dashboard（第三方觀測） | ⏳ |
| FastAPI skeleton | ⏳ |

### Phase 3 — Multi-asset & Subscription

| 項目 | 狀態 |
|------|------|
| 台指期 TWSAdapter（Shioaji） | ⏳ |
| 多資產策略支援 | ⏳ |
| 使用者/訂閱（PostgreSQL + JWT） | ⏳ |
| 版本化策略發布 | ⏳ |

---

## 4) Key Decisions

見 `decisions/` 目錄：
- `2026-03-26-platform-architecture.md`
- `2026-03-26-market-adapter-architecture.md`
- `2026-03-26-performance-metrics-standard.md`
- `2026-03-26-backtest-performance-optimization.md`
- `2026-03-26-dashboard-data-scope.md`
- `2026-03-25-dashboard-architecture.md`

---

## 5) Refactor 門檻

觸發任 2~3 條才考慮大幅重構：
1. 策略 >10 且重複邏輯 >40%
2. runner 效能成瓶頸（考慮 Numba JIT）
3. 需要 tick/orderbook 回測（考慮 NautilusTrader）
4. 訂閱者 >50 且 API 延遲成瓶頸
5. 資料源 >3 導致 adapter 不一致
