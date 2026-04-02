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
- [ ] 重建 DB → 6 張表
- [ ] backtest → signal_outcomes + params 有資料
- [ ] sim → signal_outcomes 每 bar 寫入
- [ ] grep `strategy_signals` → 0 結果
- [ ] grep `write_signal[^_]` → 0 結果

---

### Step 2：Signal Monitor Dashboard

**目標：** Signal Monitor dashboard 上線。

**前置：** Step 1 部署且有 signal_outcomes 資料。

| 檔案 | 動作 |
|---|---|
| `app/grafana/.../signal_monitor.json` | 確認 SQL 與 signal_outcomes schema 對齊 |
| `app/grafana/generate_dashboards.py` | signal_monitor 生成邏輯 |

**驗收：**
- [ ] Signal Monitor 全部 panel 有資料
- [ ] 切換 `$strategy` / `$symbol` / `$n` / `$k` 正常

---

### Step 3：統一資料抓取層（未來）

**目標：** 整合兩套 fetcher + 兩層 cache 為統一入口。

**依據：** [2026-04-01 OHLCV 遷移](../decisions/2026-04-01-ohlcv-migrate-to-timescaledb.md) 架構設計

| 動作 | 檔案 |
|---|---|
| 新建 `data/market_data.py` — `get_ohlcv()` 統一入口 | 新建 |
| 吸收 `data/binance.py` 的 HTTP 分頁 + `pipeline/fetchers/` 的 retry 邏輯 | 合併 |
| `strategies/*/utils.py` 改用 `get_ohlcv()` | 修改 |
| `persist_backtest()` 移除 `write_ohlcv()` — fetch 時已入 DB | 修改 |
| Sim warmup 改從 DB 讀取 | `librae/live/wiring.py` |
| 移除 `data/binance.py`、`pipeline/fetchers/`、`pipeline/features/cache_store.py` | 刪除 |

**`get_ohlcv()` 邏輯：**

```python
def get_ohlcv(symbol, timeframe, start, end, source="binance_spot"):
    """統一市場資料入口：DB → API → DB。"""
    # 1. 查 DB 已有範圍
    db_df = _query_db(symbol, timeframe, start, end)
    gaps = _find_gaps(db_df, start, end, timeframe)

    # 2. 有缺口 → API 補齊 → upsert DB
    if gaps:
        for gap_start, gap_end in gaps:
            api_df = _fetch_from_api(symbol, timeframe, gap_start, gap_end, source)
            _upsert_db(api_df, symbol, timeframe, source)
        db_df = _query_db(symbol, timeframe, start, end)

    return db_df
```

**DB 不可用 fallback：** 直接打 API，寫 parquet 暫存，log warning。開發環境無 DB 時仍可運作。

**驗收：**
- [ ] `get_ohlcv()` 首次呼叫 → API + DB 寫入
- [ ] 第二次相同參數 → 純 DB 讀取，不打 API
- [ ] DB 斷線 → fallback API + parquet，log warning
- [ ] `data/binance.py` 刪除後無 import error

---

## 依賴關係

```
Step 1 (Schema + Code) ✅ 已完成
  │
  ├──→ Step 2 (Dashboard)
  │
  └──→ Step 3 (統一資料層) ← 獨立於 Step 2，可平行
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
