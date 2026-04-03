# docs/ 資料夾分類準則

## decisions/

記錄當下的技術決策與思考過程。每份文件捕捉「在那個時間點，我們為什麼做這個選擇」，包含背景分析、方案比較、與最終決定。

- 以日期命名（`YYYY-MM-DD-主題.md`）
- 決策一旦寫下不回溯修改原文，保留當時的判斷脈絡
- 若實作過程與原始規劃有差異，在文件末尾新增 `## 實作差異` section 記錄偏離原因與實際做法

**文件開頭必填欄位（每項獨立一行）：**

```
> 狀態：proposed | accepted | implemented | superseded
> 範圍：影響的模組或系統（e.g. schema, engine, grafana）
> 前置決策：相關的 decision 連結（如有）
> 動機：一句話說明為什麼需要這個決策
```

**選填欄位（視情況使用）：**

```
> 取代者：指向取代本決策的新 decision 連結（superseded 時必填）
> 注記：補充說明目前落地狀態或已過時的部分
```

狀態定義：

| 狀態 | 說明 |
|------|------|
| `proposed` | 提案中，尚未確認 |
| `accepted` | 已確認方向，尚未實作 |
| `implemented` | 已實作完成 |
| `superseded` | 被後續決策取代，必須標注取代者 |

### 現有決策索引

截至 2026-04-03 整理。

| 檔案 | 狀態 | 範圍 | 摘要 |
|------|------|------|------|
| [03-06 核心決策整理](decisions/2026-03-06-core-tooling-and-schema.md) | implemented | 工具分工, 指標, 命名 | Streamlit=回測分析、Grafana=監控；9 項績效指標；snake_case 命名規範 |
| [03-25 Dashboard Architecture](decisions/2026-03-25-dashboard-architecture.md) | superseded | dashboard | Streamlit/Grafana 分工原則 → 被 03-06、04-02 signal monitor 取代 |
| [03-26 回測效能優化](decisions/2026-03-26-backtest-performance-optimization.md) | superseded | engine | Lumibot M1 瓶頸分析 → 被 04-01 取代（Lumibot 已換為 librae） |
| [03-26 Dashboard Data Scope](decisions/2026-03-26-dashboard-data-scope.md) | superseded | dashboard, db | InfluxDB tag 分離 backtest/live → 被 04-02 consolidation 取代（已遷移 TimescaleDB） |
| [03-26 MarketAdapter 架構](decisions/2026-03-26-market-adapter-architecture.md) | accepted | adapter, execution | Adapter+Hub 抽象層，解耦 signal_engine 與市場工具（CCXT/ib_insync/Shioaji） |
| [03-26 績效指標標準](decisions/2026-03-26-performance-metrics-standard.md) | accepted | metrics | 雙軌制：metrics.py (trade-based) + quantstats (equity-based)；年化因子標準化 |
| [03-26 平台架構](decisions/2026-03-26-platform-architecture.md) | accepted | architecture | 四層分離（signal_engine → vectorbt → bar-by-bar → 執行層）；執行層描述過時（Lumibot → librae） |
| [03-27 回測引擎重構](decisions/2026-03-27-backtest-engine-refactor.md) | implemented | engine | 統一 librae backtest engine，CostModel 抽象，QuantStats 整合 |
| [03-28 策略資料夾規範](decisions/2026-03-28-strategy-folder-convention.md) | implemented | strategies | `strategies/<name>/` 標準結構（strategy.py / utils.py / run.py） |
| [03-30 TSDB Bind 可配置](decisions/2026-03-30-tsdb-bind-configurable.md) | implemented | deploy | TSDB_BIND 環境變數，docker-compose port binding 可配置 |
| [03-31 DB Schema 優化](decisions/2026-03-31-database-schema-optimization.md) | superseded | schema, db | 6 表架構現況與 P0-P3 優化方向 → P0 tax 已落地，其餘移至 04-02 |
| [04-01 回測引擎優化](decisions/2026-04-01-backtest-engine-optimization.md) | accepted | engine, data | librae vs vectorbt 定位；SL/TP、short proceeds bug、cache trim 待實作 |
| [04-01 OHLCV 遷移](decisions/2026-04-01-ohlcv-migrate-to-timescaledb.md) | superseded | data, db | 提議新建 market_data 表 → 未實作，04-02 改為修改現有 ohlcv 表（移除 run_id） |
| [04-02 DB Schema 整合](decisions/2026-04-02-db-schema-consolidation.md) | implemented | schema, db, engine, grafana | 7→6 表精簡；新增 signal_outcomes；ohlcv 去重；params JSONB；寫入流程統一 |
| [04-02 Signal Monitor 審查](decisions/2026-04-02-signal-monitor-dashboard-review.md) | accepted | grafana, signal | 訊號預測力指標（IC/IC_IR/Balanced Accuracy）；Grafana=監控 / Streamlit=診斷 |

## plans/

整合一或多份 decisions 的內容，經過優化後產出的實際執行規劃。Plans 是可操作的、有步驟的實作藍圖。

- 聚焦於「怎麼做」而非「為什麼」
- 會引用相關 decisions 作為依據
- 隨實作進展持續更新，每個 Step 標注 ✅ 或待驗收

**文件開頭必填欄位（每項獨立一行）：**

```
> 狀態：planning | in-progress | completed | abandoned
> 範圍：影響的模組或系統
> 建立日期：YYYY-MM-DD
> 最後更新：YYYY-MM-DD
> 依據：相關的 decision 連結
```

## guides/

操作指南與部署文件。記錄可重複執行的步驟，供團隊成員或未來的自己參考。

## learnings/

開發過程中遭遇的問題與解法。記錄錯誤訊息、根因分析、與修復方式，避免重複踩坑。
