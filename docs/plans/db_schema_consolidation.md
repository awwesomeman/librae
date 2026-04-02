# DB Schema Consolidation — 執行計劃

> 依據：[2026-04-02 DB Schema 整合](../decisions/2026-04-02-db-schema-consolidation.md)、[2026-04-01 OHLCV 遷移](../decisions/2026-04-01-ohlcv-migrate-to-timescaledb.md)
> 策略：**DROP + 重建**（現有資料為實驗用途，不需保留）

---

## 最終 Schema：7 → 6 張表

```
backtest_runs  ← 中心表（+params JSONB, +CHECK mode）
  ├── equity_curve         (hypertable, +strategy_name)
  ├── trade_blotter        (普通表, +run_id index, +CHECK side)
  ├── strategy_performance (1 row/run, 不變)
  └── ohlcv                (hypertable, 移除 run_id 依賴)

+ signal_outcomes          (hypertable, 新建)
- strategy_signals         (刪除 — 被 trade_blotter + signal_outcomes 取代)
```

三個資料域，各自獨立：

| 域 | 表 | 用途 |
|---|---|---|
| **市場資料** | `ohlcv` | 共享價格資料，不綁 run，cache + dashboard 共用 |
| **策略績效** | `backtest_runs` + `equity_curve` + `trade_blotter` + `strategy_performance` | 回測/sim/live 的完整結果 |
| **訊號監控** | `signal_outcomes` | feature-layer 訊號記錄，搭配 ohlcv on-demand 計算 forward metrics |

---

## 資料流架構（含 cache 策略）

### 現況問題

```
API → parquet cache (6h) ──→ backtest ──→ write_ohlcv() → DB
                                                              ↓
API → JSON cache (5min) ──→ pipeline              Grafana ← DB
```

- 兩套 fetcher（`data/binance.py` + `pipeline/fetchers/`），互不知道對方
- 兩層 cache（parquet + JSON），各自為政
- DB 的 ohlcv 只被 dashboard 讀，backtest 不讀 DB
- Sim 重啟每次重拉 warmup，不利用 DB 已有資料

### 目標架構（Step 3）

```
Exchange APIs
      │
      ▼
data/market_data.py    ← 統一入口：get_ohlcv()
      │
      ├─ 1. 查 DB（有完整範圍 → 直接回傳）
      ├─ 2. DB 部分命中 → 算缺口 → API 補缺 → upsert DB → 回傳
      └─ 3. DB 不可用 fallback → API + parquet 暫存
      │
      ▼
  ┌───┼──────────┐
  ▼   ▼          ▼
回測  Pipeline  Sim/Live
```

**DB 作為 persistent store + parquet 作為離線 fallback**，統一由 `get_ohlcv()` 管理。

### 未來擴充設計

| 資料類型 | 建議方案 | 理由 |
|---|---|---|
| OHLCV（價格） | 現有 `ohlcv` 表（未來可 rename `market_data`） | 欄位固定、高頻、TimescaleDB 壓縮效率好 |
| 總經指標 | 未來新建 `macro_series (ts, indicator, source, value)` | Schema 不同（單值 vs 5 欄）、更新頻率不同 |
| 股票因子 | 留在 factorlib（Polars/parquet） | 研究階段變動頻繁，寫入 DB 過早優化 |

不在現在做 rename 或加表 — 等實際需要時再擴充。`ohlcv` 表的 `(ts, symbol, timeframe)` unique key 已足夠 general。

---

## 執行步驟

### Step 1：Schema + Python（已完成 ✅）

一次完成所有 schema 變更和 Python 程式碼修改。

#### 1a. Schema（`deploy/timescale_init.sql`）✅

| 表 | 變更 |
|---|---|
| `backtest_runs` | +`params JSONB`, +`CHECK mode` |
| `equity_curve` | +`strategy_name TEXT` |
| `trade_blotter` | +`INDEX (run_id)`, +`CHECK side` |
| `ohlcv` | `run_id` optional（無 FK），unique key = `(ts, symbol, timeframe)` |
| `signal_outcomes` | 新建 hypertable |
| `strategy_signals` | 從 DDL 移除 |

#### 1b. 刪除不需要的檔案 ✅

- `deploy/migrations/v1_1_0_consolidation.sql`
- `db/migrate.py`

#### 1c. Writer 層（`db/timescale_writer.py`）✅

- 新增 `write_signal_outcome()` + `persist_backtest()`
- 修改 `write_backtest_output()` +signal_series/params，移除 strategy_signals 寫入
- 修改 `write_run_metadata()` +params_json，`import json` 移至 top-level
- 修改 `write_ohlcv()` run_id optional
- 修改 `write_equity_point()` +strategy_name
- 刪除 `write_signal()`

#### 1d. Dataclass（`librae/backtest/schema.py`）✅

- `RunMetadata` +`params`，`EquityCurvePoint` +`strategy_name`

#### 1e. Live Engine（`librae/live/engine.py`）✅

- 移除 `on_signal`，新增 `on_signal_outcome` + `signal_column`
- `_process_bar()` feature-layer signal 捕捉，用 `pd.isna()`

#### 1f. Wiring（`librae/live/wiring.py`）✅

- 移除 `on_signal` + `write_signal` import
- 新增 `on_signal_outcome_cb`，`on_bar` 帶 strategy_name，`on_ohlcv` 不帶 run_id

#### 1g. Strategy Callers ✅

- 提取 `persist_backtest()` 共用函式，兩個 run.py 各一行呼叫

#### 1h. Reader 層（`db/timescale_reader.py`）✅

- `load_strategy_signals()` 改查 `trade_blotter`
- `load_ohlcv()` 支援 `symbol/timeframe/start_ts/end_ts`

#### 1i. Grafana ✅

- Entry/exit SQL 改查 `trade_blotter`

**驗收：**
- [x] `tests/engine/` 102 passed
- [x] grep `write_signal[^_]` → 0 結果
- [x] strategy_dashboard.json 重新生成，不再引用 strategy_signals
- [ ] 重建 DB → 6 張表（部署時驗收）
- [ ] backtest → signal_outcomes + params 有資料（部署時驗收）
- [ ] sim → signal_outcomes 每 bar 寫入（部署時驗收）

---

### Step 2：Signal Monitor Dashboard ✅

**目標：** Signal Monitor dashboard 上線。

| 項目 | 狀態 |
|---|---|
| `signal_monitor.json` SQL 與 signal_outcomes schema 對齊 | ✅ 11 panels（7 stat + 4 timeseries），全部引用 signal_outcomes + ohlcv |
| `strategy_dashboard.json` 重新生成（消除 strategy_signals 引用） | ✅ |
| signal_monitor 不由 generate_dashboards.py 生成（手動維護 JSON） | 確認 |
| Template variables: `$strategy`, `$symbol`, `$timeframe`, `$n`, `$k`, `$expected_direction` | ✅ 全部從 signal_outcomes 動態查詢 |

**驗收（部署後）：**
- [ ] Signal Monitor 全部 panel 有資料
- [ ] 切換變數正常

---

### Step 3：統一資料抓取層 ✅

**目標：** 整合兩套 fetcher + 兩層 cache 為統一入口。

**依據：** [2026-04-01 OHLCV 遷移](../decisions/2026-04-01-ohlcv-migrate-to-timescaledb.md) 架構設計

| 動作 | 狀態 |
|---|---|
| 新建 `data/market_data.py` — `get_ohlcv()` 統一入口（DB → API gap-fill → DB） | ✅ |
| `strategies/*/utils.py` 改用 `get_ohlcv()` | ✅ |
| `persist_backtest()` 保留 `write_ohlcv()` 作為 safety net | ✅ |
| Sim warmup 從 DB 讀取（`warmup_fetcher`，動態計算 months） | ✅ |
| `data/binance.py` 加入 exponential backoff + Retry-After | ✅ |
| `interval_to_timedelta()` 提取到 `librae/core/utils.py` 共用 | ✅ |
| 刪除 `pipeline/fetchers/binance_fetcher.py`（重複） | ✅ |
| 刪除 `pipeline/features/cache_store.py`（JSON cache 被 DB 取代） | ✅ |
| `core_data_sources.py` spot/futures 委派 `get_ohlcv()` | ✅ |

**驗收（部署後）：**
- [ ] `get_ohlcv()` 首次呼叫 → API + DB 寫入
- [ ] 第二次相同參數 → 純 DB 讀取，不打 API
- [ ] DB 斷線 → fallback API，log warning

---

## 依賴關係

```
Step 1 (Schema + Code) ✅
  │
  ├──→ Step 2 (Dashboard) ✅
  │
  └──→ Step 3 (統一資料層) ✅
```

## 部署流程（Step 1）

```bash
# 1. 停止 sim process
# 2. DROP + 重建 DB
psql -U quant -d quant -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
psql -U quant -d quant -f deploy/timescale_init.sql
# 3. 部署新程式碼
# 4. 重跑 backtest 填充資料
python -m strategies.trendpullback.run --mode backtest
# 5. 啟動 sim
python -m strategies.trendpullback.run --mode sim
```

## 命名慣例：`xxx_data`

時間序列資料表統一採用 `xxx_data` 命名。每張表獨立負責自己的 cache + persistent store。

| 表名 | 用途 | 建立時機 |
|---|---|---|
| `ohlcv_data` | 價格資料（現 `ohlcv`，Step 3 rename） | Step 3 |
| `macro_data` | 總經指標 `(ts, indicator, source, value)` | 第一次需要存總經數據時 |
| `factor_data` | 因子值（若需 dashboard） | 因子研究穩定且需要 dashboard 時 |

每張表各自有 `(ts, identifier, source)` 的 unique key，各自管 TTL 和 gap-fill。不用萬用表塞所有時間序列 — schema、更新頻率、查詢模式不同，分開更乾淨。

## 不在範圍

- 總經/因子資料表 — 等有實際需求再設計
- Migration 自動化 — 重建策略下不需要
- ThreadedConnectionPool、Alembic、retention policy — 規模化後再評估
- Signal Monitor 方法學改進（M1-M4, N1-N3）→ factorlib Streamlit
