# quant-strategy-lab Implementation Plan

> Updated: 2026-03-26
> Architecture: Signal Engine-first（pure function）+ 模組化分工
> Status: Phase 0 收尾中，重構進行中

---

## 1) Goal Alignment

### End goal
Signal subscription platform for futures, crypto, pair trading, stock selection.

### Current phase goal
Single-asset, verifiable, monitorable MVP with correct performance metrics:
1. signal_engine pure function（single truth）
2. 完整績效計算（含成本模型）
3. Streamlit 回測儀表板
4. Grafana 即時監控儀表板（只放 live 資料）
5. 驗證策略正確性（look-ahead bias、成本、三段式 OOS）

---

## 2) Tech Stack（最新）

| Area | Tool | Rationale |
|------|------|-----------|
| Signal Engine | pure Python/Pandas + pandas_ta | Single truth，pure function，無框架依賴 |
| 研究/參數掃描 | vectorbt（開源版 → 視需求升 Pro） | 多 TF、停損停利、向量化快速掃描 |
| 高保真回測 | 自建 bar-by-bar runner（含成本模型） | 訂閱者績效標準，DST-safe |
| 執行層 | CCXT（crypto）/ ib_insync（美股）/ Shioaji（台指） | 各市場最穩定工具 |
| Time-series storage | InfluxDB 2.x | 監控/訊號時序 |
| 指標展示（報告） | quantstats（橋接）| 業界標準 tearsheet |
| 指標計算（內部） | metrics.py（pluggable registry）| InfluxDB 寫入 |
| Dashboards | Streamlit（回測分析）+ Grafana（即時監控）| 職責分離 |
| Alerts | Grafana Alerting → Telegram | 24/7 無人值守 |
| Testing/CI | pytest + GitHub Actions | core / tw-live 分流 |
| Deployment | docker-compose | InfluxDB + Grafana |

### Dashboard 資料範圍
- **Grafana**：只放即時監控資料（live/sim runner 持續寫入）
- **Streamlit**：只放回測分析資料（一次性回測輸出）

### 績效指標計算標準
詳見 `decisions/2026-03-26-performance-metrics-standard.md`

---

## 3) Phase 進度追蹤

### Phase 0 — Foundation & E2E（收尾中）

| 交付項目 | 狀態 |
|---------|------|
| docker-compose（InfluxDB + Grafana） | ✅ 完成 |
| Canonical schema | ✅ 完成 |
| 可擴展績效指標模組（metrics.py） | ✅ 完成 |
| TrendPullback BTC 策略核心 | ✅ 完成 |
| Backtest runner（自建向量化）| ✅ 完成（DST bug 修復） |
| Sim signal runner | ✅ 完成 |
| Lumibot PoC（100% 訊號一致率） | ✅ 完成 |
| Grafana MVP 監控儀表板 | ✅ 完成 |
| Streamlit 回測儀表板 | ✅ 完成 |
| Telegram adapter（feature flag） | ✅ 完成 |
| Binance 真實資料抓取 | ✅ 完成 |
| Parquet 歸檔 | ✅ 完成 |
| 目錄結構扁平化（nautilus_lab → quant_lab） | ✅ 完成 |
| CI 分流（core / tw-live） | ✅ 完成 |
| **signal_engine pure function 抽出** | ❌ 待執行 |
| **pandas_ta 統一指標庫** | ❌ 待執行 |
| **成本模型（commission + slippage）** | ❌ 待執行 |
| **parity test（新舊指標對比）** | ❌ 待執行 |

### Phase 0 重構 Sprint（當前任務）

**Sprint 1（影響正確性）**
1. 抽出 `quant_lab/signal_engine/trendpullback.py`（pure function）
2. 加 `commission_bps` + `slippage_bps` 參數
3. parity test（新舊 runner 對比）
4. runner 呼叫 signal_engine，不再內嵌邏輯

**Sprint 2（架構完整）**
5. 指標計算換 pandas_ta
6. quantstats 橋接（`compute_tearsheet`）

### Phase 1 — Experiment tracking（Day 60）

| 交付項目 | 狀態 |
|---------|------|
| MLflow server 整合 | 待執行 |
| ≥2 策略可比較 | 待執行 |
| Streamlit 策略漂移偵測（回測 vs 即時）| 待執行 |
| 回歸基準測試上 CI | 待執行 |

### Phase 2 — Scheduling, notifications, API（Day 90）

| 交付項目 | 狀態 |
|---------|------|
| 排程（cron/Prefect） | 待執行 |
| Telegram 訊號推播（正式版） | 待執行 |
| Grafana 告警規則 | 待執行 |
| FastAPI skeleton | 待執行 |

### Phase 3 — Multi-asset & subscription platform

| 交付項目 | 狀態 |
|---------|------|
| 多資產策略支援 | 待執行 |
| 使用者/訂閱（PostgreSQL + JWT） | 待執行 |
| 版本化策略發布 | 待執行 |

---

## 4) Key Architecture Decisions

詳細見 `decisions/` 目錄：
- `2026-03-26-platform-architecture.md`：整體架構分工
- `2026-03-26-performance-metrics-standard.md`：績效計算標準
- `2026-03-26-backtest-performance-optimization.md`：回測效能策略
- `2026-03-26-dashboard-data-scope.md`：儀表板資料範圍
- `2026-03-25-dashboard-architecture.md`：Grafana vs Streamlit 分工

---

## 5) When to refactor（大幅重構門檻）

觸發任 2~3 條：
1. 策略 >10 且重複邏輯 >40%
2. 自建 runner 效能成瓶頸（考慮 Numba JIT）
3. 需要 tick/orderbook 回測（考慮 NautilusTrader）
4. 訂閱者 >50 且 API 延遲成瓶頸
5. 資料源 >3 導致 adapter 不一致

---

## 6) Principles

- signal_engine = single truth（pure function）
- 回測和 live 都呼叫同一個 signal_engine
- Grafana = 即時監控；Streamlit = 回測分析（不混用）
- 成本模型必須納入（commission + slippage）
- 指標計算：quantstats（展示）/ metrics.py（InfluxDB）
- 先可用再擴展；先正確再優化
