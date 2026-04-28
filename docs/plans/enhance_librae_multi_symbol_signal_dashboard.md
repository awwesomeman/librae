# Multi-Symbol Strategy Signal & Dashboard Design

> 建立日期：2026-04-06
> 最後更新：2026-04-09
> 依賴：`docs/plans/enhance_librae_multi_symbol_support.md`
>   - Phase 1-3（DB/Dashboard/Signal Monitor）：**無依賴**，不需要 engine 層多標的支援
>   - Phase 4（端到端驗證）：**Hard dependency** — 需要 engine 層 Issue 1-3 完成

## Context

`enhance_librae_multi_symbol_support.md` 解決了 engine 層的多標的對齊問題（watermark alignment、cross-sectional `on_bar`）。完成後，librae 就能支援兩大類多標的策略：

1. **套利策略 (Arbitrage/Pairs)** — 兩個以上標的之間的價差/統計套利
2. **選股策略 (Stock Selection)** — 跨截面排序，買入 top-K / 賣出 bottom-K

但目前的 **訊號資料模型**、**策略儀表板**、**訊號儀表板** 都是針對「單標的 → 單訊號 → 單方向」設計的，無法有效觀測這兩類策略。本計劃針對此議題做初步規劃。

---

## 1. 現狀分析

### 1.1 Signal Data Model

**`signal_events` 表 (`deploy/timescale_init.sql:123-141`)**
```
ts, run_id, strategy, symbol, mode, timeframe, signal_value, price, signal_type
UNIQUE: (ts, strategy, symbol, mode, timeframe, signal_type)
```

問題：
- **單標的粒度** — 每列 = 一個 symbol 的一個訊號，無法表達「同一決策點下多標的聯動」
- **無決策群組** — 套利的「買 A + 賣 B」是同一個決策，但目前無法關聯
- **缺乏元資料** — 無法記錄 spread value、z-score、rank、weight 等多標的訊號特有資訊
- **signal_value 語意單一** — 只有 1.0 / -1.0，無法表達排名分數或連續型訊號強度

### 1.2 Strategy Dashboard (`app/grafana/generate_dashboards.py`)

| 面板 | 問題 |
|------|------|
| **Price Trend** (L337-368) | `WHERE o.symbol = m.symbol` — 只查一個 symbol |
| **Unrealized PnL** (L517-570) | 用 `meta.symbol` 取最新 close — 多標的時只能看到 primary symbol |
| **Current Position** (L551-570) | `ORDER BY ts DESC LIMIT 1` — 只能顯示最後一筆 event 的部位，多標的時會遺漏其他 symbol 的部位 |
| **Entry/Exit Signals** (L443-497) | 訊號點對應單一價格軸 — 套利的 Entry 是在 spread 上，不是在單一標的上 |
| **Trade Events** (L369-442) | 已有 symbol 欄位，但缺乏「同組交易」的視覺分群 |
| **Equity Curve** (L276-301) | portfolio-level，**已適用**多標的 |
| **Drawdown** (L303-333) | portfolio-level，**已適用**多標的 |
| **KPI** (L162-270) | portfolio-level，**已適用**多標的 |

### 1.3 Signal Monitor Dashboard (`generate_dashboards.py:832-1012`)

| 面板 | 問題 |
|------|------|
| **Price & Signals** (L909-943) | 只畫一個 symbol 的 close，signal dots 疊在上面 |
| **Forward Return / MFE / MAE** | LATERAL JOIN 用單一 symbol OHLCV — 套利的 forward return 應基於 spread，選股應基於 portfolio |
| **Cumulative Signal Return** (L944-959) | 單標的 return 的累加 |
| **Rolling Mean / Edge Ratio** | 同上 |

---

## 2. 多標的策略類型分析

### 2.1 套利策略 (Pairs / Statistical Arbitrage)

**訊號本質**: 訊號發生在 **spread** 層級（如 z-score 觸及閾值），不在個別標的層級。

**一次決策產出**:
- Spread signal: z_score = -2.1, spread_value = 0.035
- Leg A: buy BTCUSDT qty=1
- Leg B: sell ETHUSDT qty=15

**需要觀測的指標**:
| 類別 | 指標 |
|------|------|
| 訊號品質 | Spread forward return、Spread MFE/MAE（TODO：需預計算 spread OHLCV）、Edge Ratio (spread-based) |
| 部位追蹤 | 雙腿 unrealized PnL（各腿 + 淨值）、Spread convergence 追蹤 |
| 歷史分析 | Spread 時序圖 + signal overlay、Per-leg return decomposition |

### 2.2 選股策略 (Cross-Sectional / Stock Selection)

**訊號本質**: 訊號發生在 **截面** 層級（ranking/scoring），每個 bar 對所有 symbol 打分後選出 basket。

**一次決策產出**:
- Selection signal: 選出 [AAPL, MSFT, GOOG, AMZN, META]
- Per-symbol: rank=1 weight=0.25, rank=2 weight=0.22, ...
- Rebalance actions: buy X, sell Y, hold Z

**需要觀測的指標**:
| 類別 | 指標 |
|------|------|
| 訊號品質 | Portfolio forward return（equal-weight or signal-weighted）、Top-K vs Bottom-K spread、**IC (Information Coefficient)**、**IC Decay**、**Quantile Return Spread** |
| 換手追蹤 | Turnover rate、Basket stability（overlap between consecutive selections） |
| 歸因分析 | Per-symbol contribution to portfolio return、Sector/factor exposure |

---

## 3. 設計方案

### 3.1 Signal Data Model 擴展

在 `signal_events` 增加兩個欄位：

```sql
ALTER TABLE signal_events
  ADD COLUMN signal_group_id TEXT,          -- 同一決策點的多標的訊號共享
  ADD COLUMN signal_meta     JSONB;         -- 策略特定元資料
```

**signal_group_id**:
- 套利: 買腿 + 賣腿共享同一 group_id (e.g., `arb-{ts}-{uuid8}`)
- 選股: 同一期被選中的所有 symbol 共享同一 group_id (e.g., `sel-{ts}-{uuid8}`)
- 單標的策略: NULL（向後相容）
- **寫入原子性**：同組信號必須在同一 transaction 內 batch 寫入，避免 orphan group

**signal_meta** — 策略端顯式構建 `signal_meta` dict，writer 不負責推斷：

```python
# Python 層 TypedDict 驗證，DB 層保持 JSONB 彈性
class PairsSignalMeta(TypedDict):
    leg: Literal["long", "short"]
    spread_value: float
    z_score: float
    hedge_ratio: float

class SelectionSignalMeta(TypedDict):
    rank: int
    score: float
    weight: float
    basket_size: int
```

**新增索引**:
```sql
CREATE INDEX idx_signal_events_group ON signal_events(signal_group_id, ts DESC)
    WHERE signal_group_id IS NOT NULL;

-- 高頻 JSONB 查詢的 expression index
CREATE INDEX idx_signal_meta_leg ON signal_events ((signal_meta->>'leg'))
    WHERE signal_meta IS NOT NULL;
```

**Migration 注意**：ALTER TABLE ADD COLUMN (NULL default) 不阻塞；index 使用 `CREATE INDEX CONCURRENTLY` 避免 lock table。

**Unique index 不需改動** — 原有 `(ts, strategy, symbol, mode, timeframe, signal_type)` 仍然有效，因為每個 symbol 在同一時間點仍然只有一個 entry/exit signal。

### 3.2 Feature / Signal Pipeline 變更

**`db/timescale_writer.py`**:
- `write_signal_event()` 支援 batch 寫入（同一 transaction 內寫入同組信號）
- `_extract_signals()` 需支援從 DataFrame 讀取 `signal_group_id` 和 `signal_meta` 欄位
- 新增 helper: `generate_signal_group_id(strategy_type: str, ts: datetime) -> str`

**`librae/live/signal_poller.py`**:
- `feature_fn` 回傳的 DataFrame 若含 `signal_group_id` / `signal_meta` 欄位，自動寫入

**策略端約定**:
- 策略端在 `feature_fn` 回傳時顯式構建 `signal_meta` dict（符合對應 TypedDict schema）
- Writer 直接寫入 `signal_meta` 欄位，不做欄位推斷或打包

### 3.3 Strategy Dashboard 新增/修改面板

按 strategy type 生成獨立 dashboard（`generate_strategy_dashboard(strategy_type)`），不使用 Grafana variable 動態切換面板。現有架構已是 code-gen，拆分成本低，維護性更好。

#### 3.3.1 Multi-Symbol Price (替換 Price Trend)

多標的時使用 normalized overlay + Spread panel：

```sql
-- Normalized price (base=100)
WITH meta AS (...),
     syms AS (SELECT unnest(string_to_array(m.symbol, ',')) AS sym FROM meta m),
     first_price AS (
       SELECT DISTINCT ON (o.symbol) o.symbol, o.close AS base
       FROM ohlcv o, meta m, syms s
       WHERE o.symbol = s.sym AND ...
       ORDER BY o.symbol, o.ts
     )
SELECT o.ts AS time,
       o.symbol || ' (norm)' AS metric,
       o.close / fp.base * 100 AS value
FROM ohlcv o
JOIN first_price fp ON o.symbol = fp.symbol
WHERE ...
```

#### 3.3.2 Spread Chart (套利專用，新面板)

Spread 計算公式存在 `backtest_runs.params`（run-level base formula），signal-level 的 `signal_meta` 存當期 dynamic ratio（如有）。Dashboard 從 params 讀取公式動態計算，不硬編碼特定公式。

```sql
-- 公式從 backtest_runs.params->>'spread_formula' 讀取
-- 範例：linear spread = A - ratio * B
WITH meta AS (...),
     spread_cfg AS (
       SELECT (params->>'hedge_ratio')::float AS ratio,
              (params->>'spread_type')::text AS spread_type
       FROM backtest_runs WHERE run_id = '${run_id}'
     ),
     prices AS (
       SELECT ts, symbol, close FROM ohlcv WHERE ...
     )
SELECT a.ts AS time,
       CASE WHEN sc.spread_type = 'log'
            THEN ln(a.close) - sc.ratio * ln(b.close)
            ELSE a.close - sc.ratio * b.close
       END AS "Spread"
FROM prices a
JOIN prices b ON a.ts = b.ts AND a.symbol != b.symbol
CROSS JOIN spread_cfg sc
WHERE a.symbol = ${symbol_a}
```

#### 3.3.3 Multi-Symbol Position Summary (替換 Current Position)

```sql
-- 所有 symbol 的最新部位
SELECT DISTINCT ON (symbol)
  symbol, side, position_quantity, entry_price
FROM trade_events
WHERE run_id = '${run_id}'
ORDER BY symbol, ts DESC
```

#### 3.3.4 Per-Symbol Return Breakdown (新面板)

```sql
SELECT symbol,
       COUNT(*) FILTER (WHERE event_type IN ('close','reduce')) AS trades,
       SUM(pnl) AS total_pnl,
       AVG(net_return) AS avg_return
FROM trade_events
WHERE run_id = '${run_id}' AND event_type IN ('close','reduce')
GROUP BY symbol
```

#### 3.3.5 Portfolio Composition Timeline (選股專用，新面板)

以 stacked bar 或 heatmap 呈現每個時間點持有哪些 symbol：

```sql
-- 每個時間點的 active positions (從 trade_events 推導)
WITH events AS (
  SELECT ts, symbol, event_type, position_quantity
  FROM trade_events WHERE run_id = '${run_id}'
),
...
```

### 3.4 Signal Monitor — 按 strategy type 生成獨立 dashboard

不使用 Grafana variable 切換面板，改為 `generate_dashboards.py` 按 strategy type 生成獨立 Signal Monitor dashboard。

#### 3.4.1 Pairs Signal Monitor

- Spread & Signals (spread 時序 + signal dots)
- Spread Forward Return
- Spread Edge Ratio
- Spread-based MFE/MAE（TODO：待 spread OHLCV 預計算方案確定）

#### 3.4.2 Selection Signal Monitor

- Portfolio NAV & Rebalance Points
- Portfolio Forward Return (equal-weight basket，初期用 equal-weight all symbols 作為 baseline，不引入外部 index)
- Turnover panel
- Top vs Bottom spread
- IC (Information Coefficient)
- IC Decay
- Quantile Return Spread

#### 3.4.3 Spread Forward Return (套利) SQL

```sql
-- 用 signal_group_id 找到同組訊號，計算 spread return
WITH meta AS (...),
grp AS (
  SELECT signal_group_id, MIN(ts) AS signal_ts,
         MAX(CASE WHEN (signal_meta->>'leg') = 'long' THEN symbol END) AS long_sym,
         MAX(CASE WHEN (signal_meta->>'leg') = 'short' THEN symbol END) AS short_sym,
         MAX((signal_meta->>'hedge_ratio')::float) AS ratio
  FROM signal_events
  WHERE run_id = '${run_id}' AND signal_group_id IS NOT NULL
  GROUP BY signal_group_id
),
...
```

#### 3.4.4 Selection Portfolio Forward Return SQL

```sql
-- 用 signal_group_id 找到同期被選中的 symbols，計算 equal-weight forward return
WITH grp_symbols AS (
  SELECT signal_group_id, ts, symbol,
         COALESCE((signal_meta->>'weight')::float, 1.0 / COUNT(*) OVER (PARTITION BY signal_group_id)) AS w
  FROM signal_events
  WHERE run_id = '${run_id}' AND signal_group_id IS NOT NULL
  GROUP BY signal_group_id, ts, symbol, signal_meta
),
-- 每個 symbol 的 n-bar forward return × weight → SUM = portfolio return
...
```

---

## 4. 實作順序 (Implementation Phases)

> 採用 schema follows usage 原則 — 先用實際策略驅動資料模型，再開發 dashboard。

### Phase 1: Spike — 最小 pairs strategy
- 寫一個 hardcode 的 BTC/ETH spread mean reversion strategy
- 用現有 backtest engine 跑通（single-run，不寫 signal_events）
- 目標：驗證 on_bar 多標的流程、確認 signal_meta 實際需要的欄位

### Phase 2: Signal Data Model (DB + Writer)
- 從 Phase 1 實際輸出反推 signal_meta schema
- `deploy/timescale_init.sql` — 新增欄位 + index（`CREATE INDEX CONCURRENTLY`）
- `db/timescale_writer.py` — batch write + signal_group_id + signal_meta
- `db/timescale_reader.py` — 新增 `load_signal_groups(run_id)` query
- 測試：signal_meta 寫入/讀取 round-trip test

### Phase 3: Strategy Dashboard Multi-Symbol Panels
- 有真實資料驗證面板正確性
- 按 strategy type 生成獨立 dashboard
- 修改 Price Trend → 多標的 normalized overlay
- 修改 Current Position → multi-symbol position summary
- 新增 Per-Symbol Return Breakdown table panel
- 新增 Spread Chart（套利 dashboard）
- 新增 Portfolio Composition Timeline（選股 dashboard）
- 所有新 SQL 加 `WHERE signal_group_id IS NOT NULL` guard，單標的走原有面板不受影響

### Phase 4: Signal Monitor Multi-Symbol Support
- Pairs dashboard: Spread & Signals、Spread Forward Return、Spread Edge Ratio
- Selection dashboard: Portfolio Return、Turnover、Top vs Bottom、IC、IC Decay、Quantile Spread
- MFE/MAE 標記為 TODO，待 spread OHLCV 預計算方案確定
- Dashboard SQL 用 test fixture 驗證
