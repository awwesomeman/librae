# Fix Look-Ahead Bias + Flexible Fill Price

> 狀態：implemented
> 範圍：engine, executor, grafana
> 建立日期：2026-04-05
> 最後更新：2026-04-06
> 依據：[2026-04-01 回測引擎優化](../decisions/2026-04-01-backtest-engine-optimization.md)

## 背景

所有執行路徑都有前視偏誤：策略在 bar T 看到完整 OHLCV 後決策，卻用 **bar T 的 close** 成交。
實際上看到 close 時 bar 已結束，最早能交易是 bar T+1。

### 現狀問題

| 路徑 | 前視偏誤 | 成交價 | 問題 |
|---|---|---|---|
| 策略回測 | `on_bar(bar_T)` → `process_actions` 用 `bar_T["close"]` | 寫死 close | 看到 close 卻用 close 成交 |
| 策略 sim | `on_bar(bar_T)` → `self._last_prices[sym]` = `bar_T["close"]` | 寫死 close | 同上，且實盤應用即時價格 |
| 訊號回測 dashboard | `_ENTRY_BAR`: `ts <= s.ts` 取 bar T close | 寫死 close | 訊號 bar 的 close 當 entry price |
| 訊號 MFE/MAE | `_EXC_CTE` entry_close: `ts <= s.ts` | 寫死 close | 同上 |

### 設計目標

1. **使用者可彈性選擇成交價**（open/high/low/close/vwap/limit price）
2. **引擎自動在下一期成交**（消除前視偏誤）
3. 適用所有路徑：策略回測、策略 sim、訊號回測/sim
4. **不改 `process_actions()` 和 `BaseStrategy.on_bar()` 的介面**

---

## 核心改動

### 1. `Action` 加入 `fill_price` 欄位

策略/訊號在設計時可以指定希望用什麼價格成交：

```python
@dataclass(frozen=True)
class Action:
    type: Literal["buy", "sell", "close", "hold"]
    symbol: str = ""
    quantity: float | None = None
    reason: str = ""
    fill_price: str | float | None = None  # 新增
    # str:   bar dict 的任意欄位名 — 取 next bar 的對應欄位值成交
    #        常用："open" / "close" / "vwap"
    #        自訂：任何在 prepare_signals 中計算的欄位（如 "twap", "mid_price"）
    # float: limit price — 若 next bar 的 high/low 涵蓋此價，以此價成交；否則不成交
    # None:  使用引擎預設（cfg.params["fill_price"]，通常是 "open"）
```

使用範例：

```python
class MyStrategy(BaseStrategy):
    def on_bar(self, ctx: Context) -> list[Action]:
        if ctx.bar["entry_signal"]:
            return [Action("buy", reason="signal",
                           fill_price="open")]       # 下一根 bar 的 open 成交
        if ctx.bar["limit_trigger"]:
            return [Action("buy", reason="limit",
                           fill_price=ctx.bar["close"] * 0.99)]  # 限價單
        return []
```

### 2. 策略回測引擎：next-bar execution

`librae/backtest/engine.py` 主迴圈改為 pending-then-execute 模式：

```python
pending_actions: list[Action] = []

for step, ts in enumerate(self._timeline):
    bars = all_bars[ts]

    # ── Step 1: 執行上一根 bar 暫存的 actions（用當前 bar 的價格）──
    #    逐 action 呼叫，因為每個 action 可能有不同 fill_price（lambda default arg capture）
    for action in pending_actions:
        result = process_actions(
            [action], positions, cash, ts,
            get_price=lambda sym, _a=action: resolve_fill_price(
                bars.get(sym, {}), _a, default_fill=self._fill_price),
            get_cost_model=self._get_cost_model,
            primary_symbol=primary_symbol,
        )
        trades.extend(result.trades)
        all_events.extend(result.events)
        cash += result.cash_delta
    pending_actions = []

    # ── Step 2: 計算 equity（已反映剛執行的交易）──
    mtm, pos_snapshot = self._eval_equity(cash, positions, bars)
    equity_curve.append(EquitySnapshot(ts=ts, equity=mtm))

    if positions:
        exposed_bars += 1

    # ── Step 3: 策略決策（產生下一根 bar 要執行的 actions）──
    ctx = Context(
        ts=ts, symbol=primary_symbol, symbols=self._symbols,
        bar=bars.get(primary_symbol, {}), bars=bars,
        positions=pos_snapshot, cash=cash, bar_index=step,
    )
    pending_actions = self._strategy.on_bar(ctx)
```

**關鍵變化**：
- `on_bar(bar_T)` 產生 actions → **暫存**，不立即執行
- bar T+1 開始時 → 用 T+1 的指定價格執行暫存的 actions
- 第一根 bar：只做決策，不執行（沒有暫存 actions）
- **最後一根 bar 的 pending_actions 被丟棄**（沒有 T+1 可以成交）

### 3. `resolve_fill_price` — 成交價解析（共用函式）

放在 `librae/core/executor.py`（與 `process_actions` 同模組），Backtest 和 LiveTrader 共用，不重複實作。

```python
def resolve_fill_price(
    bar: dict[str, float],
    action: Action,
    default_fill: str,
) -> float | None:
    """從 action.fill_price 或引擎預設解析實際成交價。

    Returns None if limit price not reachable (order rejected).
    """
    fill_spec = action.fill_price if action and action.fill_price is not None else default_fill

    if isinstance(fill_spec, (int, float)):
        limit = float(fill_spec)
        if limit <= 0:
            return None
        low, high = bar.get("low", 0), bar.get("high", 0)
        if low <= limit <= high:
            return limit
        return None  # 限價未觸及，不成交

    # WHY: 不寫死欄位清單，任何 bar dict key 都可用（含自訂欄位）
    if isinstance(fill_spec, str):
        val = bar.get(fill_spec)
        if val is not None and float(val) > 0:
            return float(val)
        logger.warning("fill_price='%s' not found or zero in bar, order rejected", fill_spec)
    return None
```

### 4. 策略 sim 引擎：下一期價格成交（原 §5）

`librae/live/engine.py` 的 LiveTrader 改動較小，因為 sim 是逐 bar polling：

**現狀**：偵測到 bar T 完成 → `on_bar(bar_T)` → 用 `bar_T["close"]` 成交
**改法**：偵測到 bar T 完成 → `on_bar(bar_T)` → **暫存 actions** → 偵測到 bar T+1 完成 → 用 `bar_T+1` 的指定價格成交

```python
# LiveTrader 主迴圈（虛擬碼）
self._pending_actions: list[Action] = []

def _on_new_bar(self, symbol: str, bar: dict[str, float], ts: datetime) -> None:
    # 同 §2 的 pending-then-execute pattern
    for action in self._pending_actions:
        result = process_actions(
            [action], self._positions, self._cash, ts,
            get_price=lambda s, _a=action: resolve_fill_price(
                bar, _a, default_fill=self._fill_price),
            get_cost_model=lambda s: self._executor.cost_model,
            primary_symbol=symbol,
        )
        self._handle_result(result, ts)  # 更新 positions/cash + 觸發 callbacks
    self._pending_actions = []

    ctx = self._build_context(symbol, bar, ts)
    self._pending_actions = self._strategy.on_bar(ctx)
```

### 5. 訊號回測/sim：forward return 對齊

訊號回測的 `compute_signal_metrics` 須對齊 next-bar 語意：

```python
def compute_signal_metrics(data: pd.DataFrame, cfg: RunConfig) -> SignalMetrics:
    """計算 signal 品質指標。
    
    Forward return 對齊規則：
    - Signal 在 bar T 產生
    - Entry price = bar T+1 的 fill_price 欄位（預設 open）
    - Forward return(k) = bar T+1+k 的 close / entry_price - 1
    - 絕不能用 bar T 的 close 作為 entry price
    """
    fill_col = cfg.params.get("fill_price", "open")
    
    # entry_price = next bar 的 fill_price 欄位
    data["_entry_price"] = data[fill_col].shift(-1)
    
    # forward return = T+1+k close / entry_price - 1
    for k in [1, 3, 5, 10]:
        data[f"fwd_ret_{k}"] = data["close"].shift(-(1 + k)) / data["_entry_price"] - 1
    ...
```

### 6. Signal Dashboard SQL 修正

`_ENTRY_BAR` 和 `_EXC_CTE` 改為取 T+1 的價格：

```python
# 修改前：取 bar T 的 close（前視偏誤）
_ENTRY_BAR = f"SELECT close FROM ohlcv, meta WHERE {_OHLCV_WHERE} AND ts <= s.ts ORDER BY ts DESC LIMIT 1"

# 修改後：取 bar T+1 的 $fill_price_field（Grafana custom variable，預設 open）
_ENTRY_BAR = f"SELECT $fill_price_field FROM ohlcv, meta WHERE {_OHLCV_WHERE} AND ts > s.ts ORDER BY ts LIMIT 1"
```

`_EXIT_BAR` 的 OFFSET 同步調整：

```python
# 修改前：T+n close（但 entry 是 T close，n=1 時 exit=T+1 → 只持有 1 bar）
_EXIT_BAR = f"SELECT close FROM ohlcv, meta WHERE {_OHLCV_WHERE} AND ts > s.ts ORDER BY ts LIMIT 1 OFFSET ($n - 1)"

# 修改後：T+1+n close（entry 是 T+1 open，n=1 時 exit=T+2 close → 持有 1 bar）
_EXIT_BAR = f"SELECT close FROM ohlcv, meta WHERE {_OHLCV_WHERE} AND ts > s.ts ORDER BY ts LIMIT 1 OFFSET $n"
```

`_EXC_CTE` 的 entry_close 同步修正：

```python
# 修改前
f"SELECT close AS entry_close FROM ohlcv WHERE {_OHLCV_WHERE} AND ts <= s.ts ORDER BY ts DESC LIMIT 1"

# 修改後（$fill_price_field 為 Grafana custom variable，預設 open）
f"SELECT $fill_price_field AS entry_price FROM ohlcv WHERE {_OHLCV_WHERE} AND ts > s.ts ORDER BY ts LIMIT 1"
```

MFE/MAE 的掃描範圍也需調整（從 T+1 開始，不含 entry bar 本身）：

```python
# 修改前：ts > s.ts（從 T+1 開始）
# 修改後：ts > entry_bar.ts（從 entry bar 之後開始）— 確保 entry bar 的 OHLC 不參與 MFE/MAE
```

### 7. Force-close 邊界處理

回測最後一根 bar 的 force-close：

```python
# 最後一根 bar 仍用當前 bar 的 close force-close（沒有 T+1）
# pending_actions（最後一根 bar 產生的新訊號）被丟棄（沒有 T+1 可成交）
if self._timeline:
    last_ts = self._timeline[-1]
    last_bars = all_bars[last_ts]
    # force-close 所有持倉，用 last_bars["close"]
    ...
    # 注意：不要執行 pending_actions
```

---

## 影響檔案

| 檔案 | 改動 |
|---|---|
| `librae/core/strategy.py` | `Action` 加 `fill_price: str \| float \| None = None` |
| `librae/core/executor.py` | 新增 `resolve_fill_price()` 共用函式 |
| `librae/backtest/engine.py` | 主迴圈改 pending-then-execute；force-close 邊界 |
| `librae/live/engine.py` | LiveTrader polling loop 加 pending actions |
| `librae/core/signal_metrics.py` | **新建模組**：forward return 對齊 next-bar + fill_price |
| `app/grafana/generate_dashboards.py` | `_ENTRY_BAR`、`_EXIT_BAR`、`_EXC_CTE` SQL 修正；panel title/description 加上 entry 基準說明 |
| `tests/` | 所有回測預期值更新（成交價從 T close → T+1 open） |

**不動的東西**：
- `process_actions()`：介面不變，`get_price` 仍是 `Callable[[str], float | None]`
- `BaseStrategy.on_bar()` 簽名：仍回傳 `list[Action]`
- `Context`：不變
- `CostModel`：不變

---

## 執行優先順序

1. **Signal dashboard SQL 修正**（`_ENTRY_BAR` / `_EXIT_BAR` / `_EXC_CTE`）— 只改 SQL，不影響回測
2. **`Action` 加 `fill_price` 欄位** — 純粹加欄位，預設 None，向後相容
3. **回測引擎 pending-then-execute** — 核心改動，需更新所有測試
4. **LiveTrader pending actions** — sim 路徑對齊
5. **`compute_signal_metrics` forward return 對齊** — 訊號路徑

---

## 實作進度

> 最後更新：2026-04-06
> 狀態：**全部完成** ✅（§5 signal_metrics 延後，見下方說明）

### 核心改動

- [x] §1 `Action` 加 `fill_price` 欄位 — PR #7 `c10cc40`
- [x] §2 回測引擎 pending-then-execute — PR #7 `1333b6f`
- [x] §3 `resolve_fill_price()` 共用函式 — PR #7 `c10cc40`
- [x] §4 LiveTrader pending actions — PR #7 `1333b6f`
- [ ] §5 `compute_signal_metrics` forward return 對齊 — 延後：目前 Grafana SQL 已在 dashboard 層實作 forward return 計算，Python 模組待需求出現時再建
- [x] §6 Signal Dashboard SQL 修正 — PR #7 `7711652` + PR #9 `3e1bc61`（加入 `$fill_price_field` 下拉 + signal_type filter）
- [x] §7 Force-close 邊界處理 — PR #7 `1333b6f`

### 測試

- [x] `test_look_ahead_bias.py` 已更新為 next-bar execution 預期值（105.5/110.5）— PR #7 `bf4227d`
- [x] 所有回測測試已更新 — PR #7 `bf4227d`
- [x] 207+ tests passing

### 已完成的相關工作（不在原計劃中）

| PR / Commit | 日期 | 改動 |
|-------------|------|------|
| PR #7 | 2026-04-06 | 完整 next-bar execution（§1-§4, §7）+ 測試更新 |
| PR #8 | 2026-04-06 | engine dedup + `bar_index`→`period_index` rename + Entry/Exit Signals panel 改決策時間 |
| PR #9 | 2026-04-06 | exit_signal 寫入 signal_events + Grafana Signal Type (Long/Short) filter |
| `66c727e` | 2026-04-04 | `data/binance.py` `_trim_range()` — 資料取得階段的未來資料洩漏修正 |
| `1b2e467` | 2026-03-30 | `test_look_ahead_bias.py` 建立 — 訊號穩定性 + daily merge 方向性驗證 |
| `bba8526` | 2026-03-26 | daily gate `merge_asof(direction='backward')` — daily→hourly forward merge 洩漏修正 |

---

## 驗證（全部通過）

- [x] `pytest tests/ -q` — 210 passed
- [x] 成交價從 T close → T+1 open，測試預期值已全部更新
- [x] `Action(fill_price="open")` 和 `Action(fill_price=100.5)` 正確解析
- [x] limit price 在 bar range 外時被 reject
- [x] 第一根 bar 不執行交易、最後一根 bar pending_actions 被丟棄
- [x] Signal dashboard entry price 使用 T+1 `$fill_price_field`
- [x] force-close 用最後 bar 的 close
- [x] Entry/Exit Signals panel 顯示決策時間（T）而非成交時間（T+1）

---

## 防呆注意

### Grafana 時間軸：決策時間 vs 執行時間

量化研究視覺化必須嚴格區分兩個時間點，**不可合併**：

**Signal Marker（訊號標記）→ 綁定 $T$（決策時間）**
- 研究員看圖時問的是「為什麼觸發？」 — MACD 在 10:00 交叉（基於 09:00-10:00 K 線 Close），
  signal scatter 就畫在 10:00。若挪到 11:00，研究員會質疑「11:00 根本沒交叉」。
- Grafana signal_events 時序圖：x = signal 產生的 timestamp（$T$），y = signal_value

**Trade / Order Marker（成交標記）→ 綁定 $T+1$（執行時間）**
- 實際成交發生在下一根 bar 的 fill_price。
- Grafana trade_events 時序圖：x = 成交的 timestamp（$T+1$），y = fill price（如 T+1 open）

**視覺效果**：
```
10:00  ▲ 買入訊號（signal marker，決策點）
       │  ← 1 個 timeframe 的延遲
11:00  ● 買入成交（trade marker，T+1 open 價格）
```

**實作影響**：
- `signal_events` 表的 `ts` 欄位保持為 $T$（決策時間），不動
- `signal_events.price` 欄位語義變更：**舊** = trigger price（T close）；**新** = entry price（T+1 fill_price）。既有 DB 資料仍為舊語義，drop + recreate 後統一為新語義
- `trade_events` 表的 `ts` 欄位為 $T+1$（成交時間），引擎 next-bar execution 自然產出
- Dashboard SQL 中 `_ENTRY_BAR` 取 T+1 的 `$fill_price_field` 是算 forward return 用的 **entry price**，
  不是把 signal marker 挪到 T+1

**Panel 標題與描述**（在 `generate_dashboards.py` 中直接寫明）：
- Panel title：`Forward Return (Entry @ T+1 Open)` / `24H Return (Next-Bar Execution)`
- Panel description（i icon tooltip）：「訊號在 $T$ 觸發，基準進場價使用 $T+1$ Open。報酬率 = close(T+1+k) / open(T+1) - 1」

**Hover 展示**（利用既有 `signal_events.price` 欄位，不加新欄位）：
研究員游標移到 signal marker 時顯示：
```
Signal Time:  10:00 (T)
Trigger Price (T Close): $60,000
Entry Price (T+1 Open):  $60,050    ← signal_events.price
Forward Return (k=1):    -0.5%      ← 基於 $60,050 計算
```
研究員抽查幾個點，確認進場價是下一根開盤價而非觸發價，即可對系統建立信任。

### 自訂 fill_price 欄位的資料依賴

`fill_price` 接受 bar dict 中的任意欄位名（不限於 OHLCV）。
若指定的欄位在 bar 中不存在或值為 0，`resolve_fill_price` 回傳 None（order rejected）並 log warning。
使用者有責任確保 `prepare_signals` 有產出對應欄位（如 vwap、twap、mid_price）。

### limit price 的成交假設

Limit price 檢查 `low <= limit <= high` 是簡化假設（假設 bar 內所有價格都可成交）。
實際上 bar 的 high/low 可能只是瞬間觸及，不一定有足夠流動性。
這在 crypto H1 級別足夠，但若擴展到低流動性市場需注意。

### 測試大量失敗的處理

改為 next-bar execution 後，**所有回測測試的預期值都會改變**。
建議：先把現有測試標記為 `@pytest.mark.xfail`，逐批更新預期值，
最後移除 xfail。不要一次性全改。

## 相關 Plan

- [`refactor_librae_api.md`](refactor_librae_api.md) — `RunConfig.params["fill_price"]` 定義引擎預設成交價。兩個計劃應同步實施。
