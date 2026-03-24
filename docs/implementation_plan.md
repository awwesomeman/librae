# quant-strategy-lab 實作執行計畫（Implementation Plan）

> 更新日期：2026-03-24  
> 專案狀態：規劃完成，準備進入執行

---

## 1) 目標對齊

### 最終目標
建立可讓使用者訂閱交易策略訊號的平台，涵蓋：
- 期貨
- 比特幣
- 配對交易
- 選股策略
- 其他可擴充策略型態

### 階段性目標（當前）
先完成「單資產、可驗證、可監控」的 MVP：
1. 使用真實樣本資料產出簡易策略
2. 計算主要績效指標
3. 產出基本回測儀表板
4. 產出基本監控儀表板
5. 驗證策略與回測結果正確性
6. 保留未來擴展到多資產/進階策略的空間

---

## 2) 技術工具選型（含取捨）

| 領域 | 工具 | 為何這樣選 |
|---|---|---|
| 回測框架 | `scripts/`（當前穩定） + `nautilus_lab/`（主線演進） | 先保留可用產線，再逐步遷移到事件驅動架構 |
| 時序資料儲存 | **InfluxDB 2.x** | 適合 equity curve / signal / drawdown / 監控指標 |
| 實驗追蹤 | **MLflow** | 管理 params、摘要績效、artifact，利於版本比較與重現 |
| InfluxDB vs MLflow | **各司其職** | InfluxDB 管「時間序列點資料」，MLflow 管「實驗治理」 |
| API | **FastAPI** | 快速服務化、契約清楚、易與 Pydantic 整合 |
| 儀表板 | **Grafana + Streamlit** | Grafana 做監控與告警；Streamlit 做回測互動分析 |
| 監控告警 | Grafana Alerting（可接 Telegram） | 減少自建告警複雜度 |
| 排程 | 先 `cron`，後續 `Prefect` | 先求穩可用，規模後再升級工作流 |
| 測試/CI | `pytest` + GitHub Actions | 建立回歸、契約、整合測試 gate |
| 部署 | `docker-compose` 起步 | MVP 快速落地，後續視負載再升級 |

---

## 3) Phase 0~3 執行計畫

## Phase 0（1.5~2 週）：打底與端到端打通

### 目標
讓一條單資產策略可完成完整鏈路：
回測 → 指標輸出 → InfluxDB → Grafana 顯示。

### 交付物
1. `docker-compose` 一鍵啟動 InfluxDB + Grafana
2. 定稿 canonical schema（measurement/tag/field）
3. 回測輸出對齊 `BacktestOutput`（含 `run_id`）
4. seed/匯入腳本（回測結果 → InfluxDB）
5. Grafana 基本面板（equity、drawdown、win rate）
6. CI 最小 smoke + contract test

### 驗收標準
- 新環境 5 分鐘內可重現 e2e
- Grafana 可見指定 `run_id` 曲線
- schema 驗證通過
- CI 全綠

### 風險
- schema 先天不穩導致後續 migration 成本高
- 來源資料品質（缺值/重複）影響回測結果可信度

---

## Phase 1（2~3 週）：實驗可追蹤與策略可比較

### 目標
建立「可比較、可重現」的策略實驗流程。

### 交付物
1. MLflow server 與 run log 串接
2. 回測自動記錄：參數、摘要績效、artifact
3. 至少兩個策略可在同頁比較（TrendPullback / MultiFactor）
4. Streamlit 分析頁（指標與資金曲線）
5. parity test（legacy vs 新架構）初版

### 驗收標準
- MLflow 可查詢與比較兩策略
- 指標可追溯到對應 `run_id`
- parity 差異在容忍範圍內（定義 epsilon）

### 風險
- run_id 關聯設計不一致造成資料孤島
- 指標定義未統一造成「比較失真」

---

## Phase 2（3~4 週）：排程化、推播化、服務化

### 目標
讓策略可以定時運行並對外提供查詢與通知。

### 交付物
1. cron（或 Prefect MVP）定時執行回測/監控任務
2. Telegram 信號推播
3. Grafana 告警規則（drawdown、心跳）
4. FastAPI skeleton（`/health`, `/signals/{strategy}`）
5. 失敗重試與最小可觀測性（log + metric）

### 驗收標準
- 任務可定時運行，失敗可告警
- API 可讀取最新策略訊號
- 推播與儀表板資料一致

### 風險
- 排程與告警漏報
- 訊號節流與重複推播問題

---

## Phase 3（6~8 週）：多資產與訂閱平台化

### 目標
從研究工具升級為可對外服務的平台。

### 交付物
1. 多資產策略支援（期貨/幣/配對/選股）
2. 使用者與訂閱資料模型（建議 PostgreSQL）
3. 認證授權（JWT）
4. 訂閱策略管理與通知路由
5. 版本化策略發布流程

### 驗收標準
- 外部使用者可訂閱指定策略並收到訊號
- 可追蹤每個策略版本與績效
- 監控告警覆蓋核心服務

### 風險
- 法規合規與責任界線
- 多市場時區/交易規則差異
- 平台穩定性與運維負擔提升

---

## 4) 何時需要「大幅重構」

滿足以下任意 2~3 項時，啟動 major refactor：

1. 策略數量 > 10，且重複邏輯占比 > 40%
2. `scripts/` 與 `nautilus_lab/` 雙軌維護成本持續 > 1.5x
3. 需要 tick/orderbook 級別回測與事件驅動一致性
4. 實際訂閱使用者數 > 50 且 API 延遲/穩定性成瓶頸
5. 多資料源 (>3) 導致 adapter 層不一致、故障率升高

重構方向：
- 回測與策略執行單線收斂到 `nautilus_lab/`
- 統一策略介面與共用 signal engine
- 將 `scripts/` 收斂為工具腳本，不承載核心邏輯

---

## 5) 30 / 60 / 90 天里程碑

### Day 30
- Phase 0 完成
- 一條策略 e2e 可重現（回測 → InfluxDB → Grafana）
- schema 與 run_id 契約固定

### Day 60
- Phase 1 完成、Phase 2 啟動
- MLflow 可比較至少 2 策略
- Streamlit 分析頁可用
- parity test 已上 CI

### Day 90
- Phase 2 完成
- 排程 + 推播 + API 基本可用
- 監控告警穩定
- 具備進入 Phase 3（平台化）的決策資料

---

## 6) 立即執行清單（Next Actions）

1. 建立 `BacktestOutput` 對齊任務（含欄位定義 + schema 驗證）
2. 新增全域 `run_id` 串聯（回測輸出/Influx/報表）
3. 建立 InfluxDB seed pipeline（回測結果匯入）
4. 建立 Grafana v0 dashboard（equity/drawdown/trade count）
5. 建立 Phase 0 的 CI gate（smoke + contract + parity stub）

---

## 7) 原則

- 每個 Phase 都要可 demo、可驗收
- 不為了「看起來先進」而過早重構
- 先可用、再可擴；先正確、再優化
