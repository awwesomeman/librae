# DB Schema 演進計畫

> 狀態：active（持續維護）
> 範圍：schema, db, engine, grafana, data
> 建立日期：2026-04-02
> 最後更新：2026-04-05
> 依據：
> - [2026-04-02 DB Schema 整合](../decisions/2026-04-02-db-schema-consolidation.md) — 原始 7→6 表整合
> - [2026-03-31 DB Schema 優化](../decisions/2026-03-31-database-schema-optimization.md) — 初始優化方向（已 superseded）
> - [2026-04-01 OHLCV 遷移](../decisions/2026-04-01-ohlcv-migrate-to-timescaledb.md)
> 策略：**DROP + 重建**（現有資料為實驗用途，不需保留）

---

## 現行 Schema：6 張表

```
backtest_runs              ← 中樞（per-run metadata）
  ├── equity_curve         (hypertable, FK CASCADE)
  └── strategy_performance (1 row/run, FK CASCADE)

trade_events               (hypertable, 獨立，自帶 strategy/mode/timeframe)
signal_events              (hypertable, 獨立，自帶 strategy/mode/timeframe)
ohlcv                      (hypertable, 獨立)
```

三個資料域：

| 域 | 表 | 用途 |
|---|---|---|
| **市場資料** | `ohlcv` | 共享價格資料，不綁 run，cache + dashboard 共用 |
| **策略績效** | `backtest_runs` + `equity_curve` + `trade_events` + `strategy_performance` | 回測/sim/live 的完整結果 |
| **訊號監控** | `signal_events` | feature-layer 訊號記錄，搭配 ohlcv on-demand 計算 forward metrics |

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
| signal_monitor 由 generate_dashboards.py 統一生成 | ✅ |
| Template variables: `$mode`, `$run_id`, `$n`, `$k`, `$expected_direction` | ✅ 與 Strategy dashboard 統一 run_id 篩選 |

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

## 命名慣例：`xxx`

時間序列資料表統一採用 `xxx` 命名。每張表獨立負責自己的 cache + persistent store。

| 表名 | 用途 | 建立時機 |
|---|---|---|
| `ohlcv` | 價格資料（現 `ohlcv`，Step 3 rename） | Step 3 |
| `macro` | 總經指標 `(ts, indicator, source, value)` | 第一次需要存總經數據時 |
| `factor` | 因子值（若需 dashboard） | 因子研究穩定且需要 dashboard 時 |

每張表各自有 `(ts, identifier, source)` 的 unique key，各自管 TTL 和 gap-fill。不用萬用表塞所有時間序列 — schema、更新頻率、查詢模式不同，分開更乾淨。

## 現行 Schema 欄位參考（2026-04-05 更新）

### 表間關聯

```
backtest_runs (PK: run_id)
  │ FK: run_id (ON DELETE CASCADE)
  ├── equity_curve
  └── strategy_performance

trade_events   (獨立，run_id 為普通欄位)
signal_events  (獨立，run_id 為普通欄位)
ohlcv          (獨立)
```

- 2 張表靠 FK CASCADE 管理生命週期
- 3 張獨立表自帶 strategy/mode/timeframe，刪 run 不影響
- 所有 dashboard 統一用 `run_id` 篩選

### backtest_runs（中樞，1 row / run）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `run_id` | TEXT PK | `{strategy}-{symbol}[-{timeframe}]-{ts}-{hex6}` |
| `strategy` | TEXT NOT NULL | 策略名稱 |
| `symbol` | TEXT NOT NULL | 交易標的 |
| `timeframe` | TEXT NOT NULL | K 線週期 |
| `mode` | TEXT | backtest / sim / live（CHECK） |
| `data_source` | TEXT | binance, shioaji 等 |
| `start_ts` / `end_ts` | TIMESTAMPTZ | 回測時間範圍 |
| `run_ts` | TIMESTAMPTZ | 執行時間 |
| `params` | JSONB | 策略參數快照 |
| `poll_interval` | INTEGER | sim/live polling 秒數 |
| `last_heartbeat` | TIMESTAMPTZ | sim/live 心跳 |

### equity_curve（hypertable, FK CASCADE，每 bar 一筆）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `ts` | TIMESTAMPTZ | bar 時間戳 |
| `run_id` | TEXT FK | 歸屬 run |
| `equity` | DOUBLE | 淨值 |
| `benchmark_equity` | DOUBLE | benchmark 淨值 |
| `drawdown` | DOUBLE | 回撤比例 |
| `ret_1d` | DOUBLE | 單 bar 報酬率 |
| `benchmark_ret_1d` | DOUBLE | benchmark 報酬率 |
| `strategy` | TEXT | 策略名（冗餘，便於 ad-hoc 查詢） |

### trade_events（hypertable，獨立，部位生命週期事件）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `event_id` | TEXT | `{run_id}-e{seq:04d}` |
| `run_id` | TEXT | 歸屬 run（非 FK） |
| `strategy` | TEXT NOT NULL | 策略名稱 |
| `mode` | TEXT NOT NULL | backtest / sim / live（CHECK） |
| `timeframe` | TEXT NOT NULL | K 線週期 |
| `ts` | TIMESTAMPTZ | 事件時間 |
| `symbol` | TEXT | 交易標的 |
| `side` | TEXT | long / short（CHECK） |
| `event_type` | TEXT | open / add / reduce / close（CHECK） |
| `quantity` | DOUBLE | 本次成交量 |
| `price` | DOUBLE | 成交價 |
| `entry_price` | DOUBLE | 加權平均入場價 |
| `position_quantity` | DOUBLE | 事件後剩餘持倉量 |
| `notional` | DOUBLE | 本次名目金額 |
| `commission` / `slippage` / `tax` | DOUBLE | 成本 |
| `pnl` | DOUBLE | 已實現損益（reduce/close） |
| `net_return` | DOUBLE | 淨報酬率（reduce/close） |
| `entry_ts` | TIMESTAMPTZ | 原始入場時間 |
| `holding_bars` | INTEGER | 持倉 bar 數 |
| `reason` | TEXT | 原因（strategy / force_close） |

### strategy_performance（每 run 一筆, FK CASCADE，聚合 KPI）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `run_id` | TEXT PK + FK | 1:1 對應 backtest_runs |
| `total_return` / `annual_return` | DOUBLE | 累計 / 年化報酬率 |
| `sharpe` / `sortino` / `calmar` | DOUBLE | 風險調整指標 |
| `max_drawdown` | DOUBLE | 最大回撤 |
| `win_rate` | DOUBLE | 勝率 |
| `profit_factor` | DOUBLE | 獲利因子 |
| `trades` | INTEGER | 交易筆數 |
| `avg_trade_return` | DOUBLE | 平均交易報酬率 |
| `exposure_ratio` | DOUBLE | 曝險比例 |
| `benchmark_return` | DOUBLE | benchmark 報酬率 |
| `total_commission` / `total_slippage` / `total_tax` | DOUBLE | 累計成本 |

### ohlcv（hypertable，獨立，共用市場資料）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `ts` | TIMESTAMPTZ | K 線時間戳 |
| `symbol` | TEXT NOT NULL | 交易標的 |
| `timeframe` | TEXT NOT NULL | K 線週期 |
| `source` | TEXT NOT NULL | 資料來源 |
| `open` / `high` / `low` / `close` / `volume` | DOUBLE | OHLCV |

唯一鍵：`(ts, symbol, timeframe, source)`

### signal_events（hypertable，獨立，訊號品質）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `ts` | TIMESTAMPTZ | 訊號時間 |
| `run_id` | TEXT | 歸屬 run（非 FK） |
| `strategy` | TEXT NOT NULL | 策略/訊號名稱 |
| `symbol` | TEXT NOT NULL | 交易標的 |
| `mode` | TEXT NOT NULL | backtest / sim（CHECK） |
| `timeframe` | TEXT NOT NULL | K 線週期 |
| `signal_value` | DOUBLE NOT NULL | 訊號值 |
| `price` | DOUBLE | 訊號發生時價格 |

唯一鍵：`(ts, strategy, symbol, mode, timeframe)`

## Schema 演進歷程

| 日期 | 變更 | 來源 |
|------|------|------|
| 2026-04-02 | 7→6 表：刪 strategy_signals，新增 signal_outcomes | [04-02 consolidation](../decisions/2026-04-02-db-schema-consolidation.md) |
| 2026-04-02 | ohlcv 移除 run_id 依賴，unique key 改 `(ts, symbol, timeframe, source)` | 同上 |
| 2026-04-02 | backtest_runs +params JSONB, +CHECK mode | 同上 |
| 2026-04-02 | trade_blotter +index(run_id), +CHECK side | 同上 |
| 2026-04-02 | equity_curve +strategy_name | 同上 |
| 2026-04-03 | ohlcv.run_id 欄位完全移除（schema + writer + reader） | 專案審計 |
| 2026-04-04 | backtest_runs +mode/data_source 從 RunMetadata 傳遞 | 審計 #5 |
| 2026-04-05 | +order_events hypertable（部位生命週期事件） | [position lifecycle](enhance_position_lifecycle.md) |
| 2026-04-05 | 移除 backtest_runs.schema_version + sample（YAGNI） | 專案審計 |

## 待處理

所有 Issue（1-7）已完成，見下方「Issue 完成紀錄」。目前無待處理項目。

---

## Issue 完成紀錄（2026-04-05）

### Issue 5+3 ✅ — order_events 改獨立 + 合併 trade_blotter

- trade_blotter 表刪除（7→6 表），close/reduce 事件完全取代
- order_events 移除 FK CASCADE，`run_id` 改為普通欄位（非 FK）
- order_events 新增 `strategy`, `mode`, `timeframe` 自帶欄位
- `write_trade()` 刪除，`on_trade` callback 從 LiveTrader 移除
- `refresh_performance()` 改查 `order_events WHERE event_type IN ('close', 'reduce')`
- Grafana Unrealized PnL / Current Position 改查 `order_events.position_qty`
- Issue 2 同時被解決

### Issue 6+7+1 ✅ — 統一命名 + 欄位 rename + signal_events 加 run_id

**表 rename：**
- `order_events` → `trade_events`
- `signal_outcomes` → `signal_events`

**欄位 rename：**
- `realized_pnl` → `pnl`（trade_events）
- `avg_entry_price` → `entry_price`（trade_events）
- `position_qty` → `position_quantity`（trade_events）
- `signal_ts` → `ts`（signal_events）
- `strategy_name` → `strategy`（equity_curve）
- `net_return` — 維持不改（`return` 是 Python 保留字）

**新增：**
- signal_events 加 `run_id` 欄位（普通欄位，非 FK）
- signal_events mode CHECK 移除 `live`（只允許 backtest/sim）

**函式 rename：**
- `write_signal_outcome()` → `write_signal_event()` + 新增 `run_id` 參數
- `write_order_event()` → `write_trade_event()`
- `write_trade()` → 刪除
- `load_order_events()` → `load_trade_events()` + 新增 `event_types` 篩選參數
- `load_trade_blotter()` → 刪除（由 `load_trade_events(event_types=...)` 取代）

### 設計決策：統一 run_id 篩選

**決策：所有表、所有 mode 都以 `run_id` 為主要篩選維度，不支援跨 run 累積。**

原因：
- 跨 run 累積假設「同一策略 = 同一配置」，但 sim 重啟常見原因是改參數/改邏輯，混在一起看無意義
- signal_events 也有同樣問題 — 改了 SMA 週期，訊號品質完全不同
- 統一 run_id 篩選：一個心智模型、無參數污染風險、壞資料一個 WHERE 隔離
- 代價（sim 跨重啟不連續）實務影響小，需要時用 notebook 彌補

### 刻意的設計（非 bug，不需修）

**Reader 不 SELECT 冗餘欄位：**
- `trade_events` 的 `strategy/mode/timeframe`：寫入時帶入（為 DB ad-hoc 查詢和 Grafana 提供彈性），但 Python reader 用 `run_id` 篩選、呼叫端已從 `backtest_runs` 知道這些資訊
- `equity_curve` 的 `strategy`：同理

**signal_events 無 Python reader：**
- 目前只被 Grafana SQL 直接查詢，Python 層不需要讀取
- 未來 notebook/分析需要時再加 `load_signal_events()`（YAGNI）

**Python API 保留舊名（非 DB 欄位）：**
- `strategy_name`：engine 參數名（`Backtest(strategy_name=...)`, `LiveExecutor.strategy_name`），寫入 DB 時映射為 `strategy=strategy_name`
- `avg_entry_price`：`brokers/base.py` Position dataclass — broker domain
- `realized_pnl` / `unrealized_pnl`：broker domain Position fields

### Signal Monitor 統一 run_id 篩選 ✅

Signal dashboard 原本用 `strategy/symbol/timeframe/data_source` 四層篩選，與 Strategy dashboard（`mode/run_id`）不一致。

變更：
- 變數改為 `mode → run_id`（與 Strategy dashboard 對齊）
- SQL WHERE 從 `strategy='$strategy' AND symbol='$symbol'` → `run_id='${run_id}'`
- ohlcv LATERAL JOIN 的 symbol/timeframe/source 改從 `backtest_runs` 子查詢取得
- 移除 Timeframe stat panel（run_id 已包含 timeframe 資訊）
- 多標的對比：用迴圈跑各 symbol 產生獨立 run_id，Grafana 切換 run_id 即可

### 驗收

- [x] 213 tests passed
- [x] 12 個舊名模式 grep → DB/Grafana 層零殘留
- [x] DDL ↔ Writer 欄位對齊：6 張表全部 MATCH
- [x] 資料流端到端一致性：3 條寫入路徑 `entry_price/position_quantity/pnl` 全鏈路一致
- [x] Grafana SQL 所有表名、欄位名對齊 DDL
- [x] strategy_dashboard.json + signal_monitor.json 重新生成/更新
- [x] 兩個 dashboard 篩選維度統一：`mode → run_id`

---

## 不在範圍

- 總經/因子資料表 — 等有實際需求再設計
- Migration 自動化 — 重建策略下不需要
- ThreadedConnectionPool、Alembic、retention policy — 規模化後再評估
- Signal Monitor 方法學改進（M1-M4, N1-N3）→ factorlib Streamlit
