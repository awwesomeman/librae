# 2026-04-02 — DB Schema 整合：資料表精簡與寫入流程統一

> 狀態：accepted
> 前置決策：[2026-03-31 DB Schema 優化](2026-03-31-database-schema-optimization.md)、[2026-04-02 Signal Monitor 審查](2026-04-02-signal-monitor-dashboard-review.md)
> 動機：新增訊號監控需求後，重新審視現有 6 張表 + 1 張計劃中表的資料流，發現冗餘與整合機會

## 現況分析

### 現有 6 張表 + 1 張計劃中

```
backtest_runs  ← 中心表
  ├── equity_curve         (hypertable)
  ├── trade_blotter        (普通表)
  ├── strategy_signals     (hypertable)  ← 問題表
  ├── strategy_performance (1 row/run)
  └── ohlcv                (hypertable)  ← 問題表

+ signal_outcomes (計劃中，尚未建表)
```

### 發現的問題

**問題 1：`strategy_signals` 是冗餘表**

| 模式 | 寫入方式 | 實際狀況 |
|------|---------|---------|
| Backtest | 從 `trade_blotter` 反推（每筆 trade → entry + exit 兩行） | 100% 冗餘，原始資料就在 trade_blotter |
| Sim/Live | `on_signal` callback，僅在 buy/sell/close 時觸發 | signal_strength 寫死 ±1.0，confidence 寫死 0.5，等同 trade 事件 |

消費者只有兩個：
- Strategy dashboard 的 entry/exit 標記 → 可改查 `trade_blotter`（已有 entry_ts/exit_ts, entry_price/exit_price）
- Streamlit `load_strategy_signals()` → 同上

**問題 2：`ohlcv` 因 run_id 造成 4x 資料重複**（已在 2026-03-31 P0 紀錄）

- 同一段 BTCUSDT H1 跑 4 次 → 4 × 13K = 52K 行
- unique key `(ts, symbol, timeframe, run_id)` 中 run_id 是多餘的

**問題 3：訊號發射的架構 gap**

Strategy `on_bar()` 回傳 `Action(type=buy/sell/close/hold)`，沒有 signal_value 概念。但訊號監控需要的是「策略每個 bar 的訊號值」，而非「是否進場」。

- 訊號值存在於 feature layer（例如 `entry_signal` 欄位），但目前沒有寫入路徑
- `on_signal` callback 只在 action 執行時觸發，不在 hold 時觸發
- 所以 `strategy_signals` 完全無法服務訊號監控的需求

**結論：** `strategy_signals` 既是冗餘（trade_blotter 已有 entry/exit），又不夠（缺少 hold 時的 signal_value）。應刪除，由 `trade_blotter` + 新的 `signal_outcomes` 各司其職。

---

## 方案：7 → 6 張表

| # | Table | 變化 | 用途 | Grain |
|---|-------|------|------|-------|
| 1 | `backtest_runs` | 加 `params JSONB` | Run 中樞 | 1 row / run |
| 2 | `equity_curve` | 不變 | 每 bar 淨值 | 1 row / bar / run |
| 3 | `trade_blotter` | 加 `(run_id)` index | 成交記錄 + entry/exit 標記 | 1 row / trade |
| 4 | `strategy_performance` | 不變 | 聚合 KPI | 1 row / run |
| 5 | `ohlcv` | **移除 run_id** 從 unique key | 共用市場資料 | 1 row / bar / symbol / timeframe |
| 6 | `signal_outcomes` | **新建** | 訊號品質監控 | 1 row / signal emission |
| ~~7~~ | ~~`strategy_signals`~~ | **刪除** | 被 trade_blotter + signal_outcomes 取代 | — |

每張表現在有單一、明確的職責。策略監控看 trade_blotter + equity_curve + strategy_performance；訊號監控看 signal_outcomes + ohlcv。

---

## 新表 Schema

### `signal_outcomes`

```sql
CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_ts       TIMESTAMPTZ NOT NULL,
    strategy        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    source          TEXT NOT NULL,              -- 'backtest' / 'sim'
    timeframe       TEXT NOT NULL,              -- canonical 格式 (H1, M5)
    signal_value    DOUBLE PRECISION NOT NULL,  -- 任意實數
    price           DOUBLE PRECISION            -- 訊號時的價格（便於驗證）
);
SELECT create_hypertable('signal_outcomes', 'signal_ts', if_not_exists => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_outcomes_unique
    ON signal_outcomes (signal_ts, strategy, symbol, source, timeframe);
CREATE INDEX IF NOT EXISTS idx_signal_outcomes_lookup
    ON signal_outcomes (strategy, symbol, source, signal_ts DESC);
```

設計決策：

| 決策 | 理由 |
|------|------|
| **無 run_id** | 訊號監控是 strategy+symbol 維度，跨 run 累積。Sim restart 換 run_id 不影響歷史可見性 |
| **無 signal_type** | 不區分 entry/exit/hold，那是 trade_blotter 的事。這裡只記錄「策略認為有訊號」的時刻 |
| **含 timeframe** | canonical 格式（H1, M5），與 backtest_runs / ohlcv 一致。Dashboard 用此欄位 JOIN ohlcv 並作為 template variable，不再 hardcode |
| **signal_value NOT NULL** | 只存有訊號的 bar（NaN 時間點不寫入），符合 [signal monitor decision doc](2026-04-02-signal-monitor-dashboard-review.md) |
| **含 price** | signal_ts 對應的收盤價，讓 Unrealized PnL query 不需額外 LATERAL JOIN |
| **無 fwd_return / MFE / MAE 欄位** | 全部從 ohlcv on-demand 計算，使用者可自由輸入任意 forward horizon |

### `ohlcv` 變更

```sql
-- 1. 先 dedup（既有資料有 N×重複，直接建 unique index 會失敗）
DELETE FROM ohlcv a USING ohlcv b
  WHERE a.ts = b.ts AND a.symbol = b.symbol AND a.timeframe = b.timeframe
    AND a.ctid > b.ctid;

-- 2. 重建 unique key（移除 run_id）
DROP INDEX IF EXISTS idx_ohlcv_unique;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ohlcv_unique ON ohlcv (ts, symbol, timeframe);

-- 3. run_id 改為可選，移除 FK
-- 注意：刪 backtest_run 不再 CASCADE 清理 ohlcv（正確行為 — ohlcv 是共享市場資料）
ALTER TABLE ohlcv ALTER COLUMN run_id DROP NOT NULL;
ALTER TABLE ohlcv DROP CONSTRAINT IF EXISTS ohlcv_run_id_fkey;
```

### `backtest_runs` 變更

```sql
ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS params JSONB;
```

### `trade_blotter` 變更

```sql
CREATE INDEX IF NOT EXISTS idx_trade_blotter_run_id ON trade_blotter(run_id);
ALTER TABLE trade_blotter
  ADD CONSTRAINT chk_side CHECK (side IN ('long', 'short')) NOT VALID;
VALIDATE CONSTRAINT chk_side;
```

### `backtest_runs` CHECK 約束

```sql
ALTER TABLE backtest_runs
  ADD CONSTRAINT chk_mode CHECK (mode IN ('backtest', 'sim', 'live')) NOT VALID;
VALIDATE CONSTRAINT chk_mode;
```

### `equity_curve` 變更

```sql
ALTER TABLE equity_curve ADD COLUMN IF NOT EXISTS strategy_name TEXT;
```

> **用途：** Grafana 疊多條 equity curve 做 overlay 比較時，不需 JOIN backtest_runs 即可辨識策略名稱。寫入時從 backtest_runs.strategy 帶入。

---

## 完整資料寫入流程

### Flow A：Backtest

```
run.py run_backtest()
  │
  ├─ fetch_and_prepare() → featured DataFrame（含 entry_signal 欄位）
  │
  ├─ bt.run() → bt.build_output()
  │
  └─ write_backtest_output(output, signal_series, params)    ← 新增參數
       ├─ UPSERT backtest_runs（含 params JSONB）
       ├─ batch INSERT equity_curve
       ├─ batch INSERT trade_blotter
       ├─ UPSERT strategy_performance
       ├─ batch INSERT signal_outcomes                       ← 新增
       └─ [移除: strategy_signals 反推寫入]
  │
  └─ write_ohlcv(df, symbol, timeframe)                      ← 移除 run_id 參數
       └─ ON CONFLICT (ts, symbol, timeframe) DO NOTHING
```

**signal_series 來源**：`run.py` 的 featured DataFrame 中 `entry_signal` 欄位。Boolean signal 轉 1.0，False/NaN 不寫入。

```python
# run.py 中：
signal_series = df.xs(symbol, level="symbol")["entry_signal"].astype(float)
signal_series = signal_series[signal_series > 0]  # 只保留 True/正值
counts = write_backtest_output(output, signal_series=signal_series, params=scfg["params"])
```

### Flow B：Sim / Live

```
build_live_trader(signal_column="entry_signal")              ← 新增參數
  │
  ├─ write_run_metadata() → backtest_runs（含 params）
  │
  └─ LiveTrader.run() poll loop:
       │
       ├─ _process_bar(symbol, raw_df, ts):
       │   ├─ featured = feature_fn(raw_df)
       │   ├─ bar = featured.iloc[-1].to_dict()
       │   │
       │   ├─ signal_val = bar.get(signal_column)            ← 新增：feature layer 捕捉
       │   ├─ if signal_val is not None and not NaN:
       │   │     on_signal_outcome(symbol, ts, signal_val, price)
       │   │     → write_signal_outcome() → signal_outcomes
       │   │
       │   ├─ strategy.on_bar(ctx) → actions
       │   ├─ execute actions → trade_blotter (via on_trade)
       │   ├─ on_bar → equity_curve
       │   └─ on_ohlcv → ohlcv
       │
       └─ on_heartbeat → backtest_runs.last_heartbeat
```

**關鍵設計**：訊號在 **feature layer** 捕捉（`bar.get(signal_column)`），而非 action layer（`on_signal`）。

- Feature layer 每 bar 都跑，不管 strategy 是否 hold
- Strategy layer 只決定是否交易，不負責訊號記錄
- 這解決了「strategy 只回傳 Action 不回傳 score」的架構 gap
- 未來換成 model score（連續值），只需改 `signal_column` 參數

### Flow C：Signal Monitor Dashboard (Grafana)

```
signal_outcomes  ──LATERAL JOIN──→  ohlcv
  (signal_ts,                        (ts, close, high, low)
   signal_value)                       │
                                       ├─ fwd_return: (exit_price - entry_price) / entry_price
                                       ├─ MFE: MAX((high - entry_price) / entry_price) over n bars
                                       └─ MAE: MAX((entry_price - low) / entry_price) over n bars
```

Dashboard 的 `$n`（forward horizon）和 `$k`（rolling window）是 textbox，使用者自由輸入。所有 forward-looking 指標 on-demand 計算。

### Flow D：Strategy Dashboard (Grafana)

Entry/exit 標記改查 trade_blotter（資料早就在那裡）：

```sql
-- Before (strategy_signals)：
SELECT ts AS time, price AS "Entry" FROM strategy_signals
  WHERE run_id='${run_id}' AND signal_type='entry'

-- After (trade_blotter)：
SELECT entry_ts AS time, entry_price AS "Entry" FROM trade_blotter
  WHERE run_id='${run_id}'
SELECT exit_ts AS time, exit_price AS "Exit" FROM trade_blotter
  WHERE run_id='${run_id}'
```

---

## Cache 策略

### 現有 cache（保持不變）

| Cache | 位置 | TTL | 用途 |
|-------|------|-----|------|
| OHLCV parquet | `data/cache/{symbol}_{interval}.parquet` | 6h | 避免重複 API 呼叫 |
| Feature JSON | `pipeline/features/cache_store.py` | configurable | 避免重複 feature 計算 |

### Dashboard on-demand 查詢 cache（分層策略）

| 層級 | 方式 | 何時啟用 | 成本 |
|------|------|---------|------|
| **Tier 1** | Grafana panel `cacheTimeout: 300` (5 min) | 立即 | 零 — 改 JSON 設定 |
| **Tier 2** | TimescaleDB chunk compression on ohlcv | 立即 | 低 — 一次性 DDL |
| **Tier 3** | `signal_metrics_cache` 物化表 + cron job | 未來 — 當 LATERAL JOIN > 3s | 中 — 需維護 job |

Tier 3 備用設計（不在本次實作範圍，記錄供未來參考）：

```sql
CREATE TABLE signal_metrics_cache (
    signal_ts   TIMESTAMPTZ NOT NULL,
    strategy    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    horizon     INTEGER NOT NULL,        -- forward bars
    fwd_return  DOUBLE PRECISION,
    mfe         DOUBLE PRECISION,
    mae         DOUBLE PRECISION,
    computed_at TIMESTAMPTZ DEFAULT NOW()
);
-- 由 cron job 對新 signal + 已有 ohlcv 的組合批次計算
-- Dashboard 查此表取代 LATERAL JOIN
-- 仍支援任意 horizon（只是非預算的 horizon 會 cache miss → fallback LATERAL JOIN）
```

---

## 實作分期

### Phase 1：Schema additions（無 breaking change）

| 動作 | 檔案 |
|------|------|
| CREATE `signal_outcomes` + indexes | `deploy/migrations/v1_1_0_consolidation.sql` |
| ALTER `backtest_runs` ADD `params JSONB` | 同上 |
| ALTER `ohlcv` unique key 移除 run_id | 同上 |
| CREATE INDEX on `trade_blotter(run_id)` | 同上 |
| ADD CHECK 約束：`mode IN ('backtest','sim','live')`, `side IN ('long','short')` | 同上 |
| ALTER `equity_curve` ADD `strategy_name TEXT` | 同上 |
| 更新 `deploy/timescale_init.sql` 反映完整 schema | `deploy/timescale_init.sql` |
| 實作 `db/migrate.py` — idempotent schema sync | `db/migrate.py` |

### Phase 2：新增寫入路徑（新舊並行）

| 動作 | 檔案 |
|------|------|
| 新增 `write_signal_outcome()` | `db/timescale_writer.py` |
| `write_backtest_output()` 加 signal_series, params 參數 | `db/timescale_writer.py` |
| LiveTrader 新增 `on_signal_outcome` callback | `librae/live/engine.py` |
| `wiring.py` 接線 + 加 `signal_column` 參數 | `librae/live/wiring.py` |
| `write_equity_curve()` 帶入 strategy_name | `db/timescale_writer.py` |
| 暫時保留 `on_signal` + strategy_signals（向後相容） | — |

### Phase 3：遷移讀取端

| 動作 | 檔案 |
|------|------|
| `run.py` 傳遞 signal_series 和 params | `strategies/*/run.py` |
| Strategy dashboard entry/exit 改查 trade_blotter | `app/grafana/generate_dashboards.py` |
| `load_strategy_signals()` 改為查 trade_blotter | `db/timescale_reader.py` |
| Streamlit 同步更新 | `app/streamlit/streamlit_performance.py` |

### Phase 4：移除舊程式碼

| 動作 | 檔案 |
|------|------|
| 移除 `write_signal()` function | `db/timescale_writer.py` |
| 移除 `on_signal` callback | `librae/live/engine.py`, `librae/live/wiring.py` |
| 移除 strategy_signals 反推寫入 | `db/timescale_writer.py` |
| 從 schema DDL 移除 strategy_signals | `deploy/timescale_init.sql` |
| DROP TABLE migration | `deploy/migrations/v1_1_0_consolidation.sql` |

### Phase 5：Dashboard + cache

| 動作 | 檔案 |
|------|------|
| signal_monitor.json 確認與 ohlcv 新 unique key 相容 | `app/grafana/.../signal_monitor.json` |
| 加入 Grafana `cacheTimeout` | 同上 |
| 啟用 TimescaleDB compression on ohlcv 歷史 chunks | DDL |

---

## Schema Migration 自動化

> 承接 03-31 P0「Schema migration 無自動化」問題。該問題在 TrendMaster 實驗中實際觸發（VPS 上已存在的 DB 缺少新欄位，writer 直接報錯）。

本次 consolidation 新增了更多 schema 變更（signal_outcomes 新表、ohlcv unique key 變更、CHECK 約束、equity_curve 新欄位），migration 自動化的需求更加迫切。

**方案：** 在 Phase 1 一併實作 `db/migrate.py`，啟動時自動執行 `ALTER TABLE ADD COLUMN IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`，確保已部署的 DB 與 schema 同步。不引入 Alembic（P3 等規模化後再評估），用簡單的 idempotent SQL 即可。

---

## 暫不納入（來自 03-31 尚未覆蓋的項目）

| 來源 | 項目 | 不納入理由 |
|------|------|-----------|
| P2 | `ThreadedConnectionPool` | 當前單執行緒架構無併發寫入需求 |
| P2 | `trade_blotter` 轉 hypertable | 單策略交易量遠未達數十萬筆門檻 |
| P3 | 引入 Alembic | 表結構變更頻率低，idempotent SQL 足夠 |
| P3 | Retention policy | 資料量尚小，無儲存壓力 |

---

## 驗證清單

1. 現有 `tests/engine/` 測試通過
2. Backtest 端到端：`python -m strategies.trendpullback.run --mode backtest` → signal_outcomes 有資料
3. Sim 端到端：`python -m strategies.trendpullback.run --mode sim` → signal_outcomes 每 bar 寫入
4. Strategy dashboard：entry/exit 標記從 trade_blotter 正確顯示
5. Signal monitor dashboard：所有 panel 正常
6. DB：`SELECT COUNT(*) FROM signal_outcomes` 確認資料量合理
7. DB：確認 `strategy_signals` 可安全 DROP（無其他 consumer）

## 執行計劃

→ [db_schema_consolidation.md](../plans/db_schema_consolidation.md) — 經 review 修正後的最終執行計劃

## 相關決策

- [2026-03-31 DB Schema 優化](2026-03-31-database-schema-optimization.md) — P0 全部吸收（OHLCV dedup、params JSONB、migration 自動化）；P1 吸收 trade_blotter index + CHECK 約束 + equity_curve strategy_name；strategy_signals FK 因刪表而不適用；P2/P3 規模化項目暫不納入
- [2026-04-02 Signal Monitor 審查](2026-04-02-signal-monitor-dashboard-review.md) — signal_outcomes 表設計原則、訊號模型定義
- [2026-04-01 回測引擎優化](2026-04-01-backtest-engine-optimization.md) — cache 機制、引擎層 bug fixes
