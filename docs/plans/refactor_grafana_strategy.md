# Order Detail 面板重新規劃（v3 — implemented）

> 狀態：implemented
> 範圍：grafana, schema, db
> 建立日期：2026-04-04
> 最後更新：2026-04-05
> 依據：[enhance_librae_position_lifecycle](enhance_librae_position_lifecycle.md)

## Context

引擎已支援加碼（scaling）與部分平倉（partial close），但目前的資料模型和 Grafana 面板只記錄「平倉事件」（trade_blotter，一筆 close = 一行）。看不到：
- 個別進場/加碼事件
- 策略下單理由（Action.reason 未持久化）
- 部位生命週期全貌（從開倉到全部平完）

設計調研詳見 `docs/decisions/2026-04-04-order-detail-panel-research.md`

## 設計：單表 Order Events

### `order_events` 新表（事件級，hypertable）

```
order_events
─────────────────────────────────────
event_id        TEXT NOT NULL        -- {run_id}-e{seq}
run_id          TEXT FK
ts              TIMESTAMPTZ NOT NULL -- 事件時間
symbol          TEXT
side            TEXT                 -- long / short
event_type      TEXT                 -- open, add, reduce, close
quantity        DOUBLE PRECISION     -- 本次數量（永遠正數）
price           DOUBLE PRECISION     -- 成交價
avg_entry_price DOUBLE PRECISION     -- 當前部位加權平均進場價
position_qty    DOUBLE PRECISION     -- 事件後的剩餘部位量
notional        DOUBLE PRECISION     -- price * quantity * multiplier
commission      DOUBLE PRECISION
slippage        DOUBLE PRECISION
tax             DOUBLE PRECISION
realized_pnl    DOUBLE PRECISION     -- reduce/close 的淨損益，open/add 為 NULL
net_return      DOUBLE PRECISION     -- reduce/close 的淨報酬率 %，open/add 為 NULL
entry_ts        TIMESTAMPTZ          -- reduce/close 的首次進場時間，open/add 為 NULL
holding_bars    INTEGER              -- reduce/close 的持有期數，open/add 為 NULL
reason          TEXT                 -- 來自 Action.reason

UNIQUE (event_id, ts)               -- hypertable 需要 ts 在 unique index
CHECK  (event_type IN ('open', 'add', 'reduce', 'close'))
CHECK  (side IN ('long', 'short'))
```

### Event 類型定義（B+ 對稱模型）

```
open    首次建立部位   pos_qty: 0 → N      方向無關（多空由 side 決定）
add     同方向加碼     pos_qty: N → N+M
reduce  部分縮減部位   pos_qty: N → N-M
close   全部結清部位   pos_qty: N → 0
```

### `trade_blotter` 現有表（不動，不建面板）

保留供指標計算用（StrategyMetrics），不再有獨立的 Trade Summary 面板。

---

## Grafana 面板：單一 Order Events 取代舊 Trade Detail

版面維持原樣（位置 `_x=12, _dy=0, h=15, w=12`），只換表格內容。

| 欄位 | 來源 | 說明 |
|------|------|------|
| # | ROW_NUMBER() | 序號 |
| Time | ts | 事件時間 |
| Event | event_type | open / add / reduce / close |
| Symbol | symbol | 標的 |
| Side | side | long / short |
| Qty | quantity | 本次數量（永遠正數） |
| Price | price | 成交價 |
| Avg Entry | avg_entry_price | 當前部位加權平均進場價 |
| Pos Qty | position_qty | 事件後剩餘部位 |
| Cost | commission + slippage + tax | 本次總成本 |
| Net P&L | realized_pnl | reduce/close 的淨損益，open/add 顯示 — |
| Net Return % | net_return | reduce/close 的淨報酬率，open/add 顯示 — |
| Entry Time | entry_ts | reduce/close 的首次進場時間，open/add 顯示 — |
| Periods | holding_bars | reduce/close 的持有期數，open/add 顯示 — |
| Reason | reason | 策略下單理由 |

顏色規則：
- Event 背景色：open/add 藍色系、reduce/close 橘色系
- Side: long 綠、short 紅
- Net P&L: 正值綠、負值紅

範例：

| # | Time | Event | Symbol | Side | Qty | Price | Avg Entry | Pos Qty | Cost | Net P&L | Net Return % | Entry Time | Periods | Reason |
|---|------|-------|--------|------|-----|-------|-----------|---------|------|---------|-------------|------------|---------|--------|
| 1 | 10:00 | open | BTC | long | 10 | 100 | 100.00 | 10 | 1.50 | — | — | — | — | RSI |
| 2 | 11:00 | add | BTC | long | 5 | 120 | 106.67 | 15 | 0.85 | — | — | — | — | momentum |
| 3 | 14:00 | reduce | BTC | long | 8 | 130 | 106.67 | 7 | 1.96 | 178.64 | 20.93% | 10:00 | 4 | TP 50% |
| 4 | 16:00 | close | BTC | long | 7 | 140 | 106.67 | 0 | 1.82 | 226.69 | 30.36% | 10:00 | 6 | reversal |
| 5 | 14:00 | open | ETH | short | 20 | 3500 | 3500.00 | 20 | 10.50 | — | — | — | — | bearish div |
| 6 | 16:00 | add | ETH | short | 10 | 3600 | 3533.33 | 30 | 5.10 | — | — | — | — | breakdown |
| 7 | 19:00 | reduce | ETH | short | 15 | 3400 | 3533.33 | 15 | 6.60 | 1982.50 | 3.74% | 14:00 | 5 | cover half |
| 8 | 22:00 | close | ETH | short | 15 | 3300 | 3533.33 | 0 | 6.45 | 3493.55 | 6.59% | 14:00 | 8 | target hit |

---

## 實作範圍

### 1. Schema 層
- **新增** `order_events` hypertable（`deploy/timescale_init.sql`）
- **不動** `trade_blotter` 表

### 2. Engine 層
- **修改** `librae/core/executor.py` — `process_actions()` 每個動作產出 OrderEvent
  - event_type: open / add / reduce / close
  - reduce/close 攜帶 realized_pnl、net_return、entry_ts、holding_bars
  - 攜帶 Action.reason
- **新增** `OrderEvent` dataclass（放在 `executor.py`）
- **修改** `ActionResults` — 新增 `events: list[OrderEvent]` 欄位

### 3. Schema 映射層
- **修改** `librae/backtest/schema.py` — 新增 `OrderEventRecord` dataclass
- **修改** `librae/backtest/engine.py` — build_output 映射 events

### 4. DB 層
- **修改** `db/timescale_writer.py` — 新增 `write_order_events()` 批次寫入
- **修改** `db/timescale_reader.py` — 新增 `load_order_events()` 讀取

### 5. Grafana 層
- **修改** `app/grafana/generate_dashboards.py` — 舊 Trade Detail 替換為 Order Events（單一面板，同位置同大小）

### 6. 測試
- 新增 `tests/engine/test_order_events.py`
  - 驗證 open/add/reduce/close 事件正確產出
  - 驗證 avg_entry_price / position_qty / realized_pnl / net_return 數值正確性
  - 驗證複雜場景（buy → buy → sell 一點 → buy → sell 全部）

## 驗證方式
1. 單元測試：`pytest tests/engine/test_order_events.py`
2. 整合測試：執行含加碼+部分平倉的策略回測，確認 order_events 寫入 DB
3. Grafana：重新生成 dashboard JSON，確認面板正確渲染
