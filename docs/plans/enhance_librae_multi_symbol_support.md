# Multi-Symbol LiveTrader Support

> 狀態：planning
> 範圍：engine, live
> 建立日期：2026-04-05
> 最後更新：2026-04-09
> 依據：[enhance_librae](enhance_librae.md) — 整合索引

## Context

`build_live_trader` 的 `symbols` 參數接受 `list[str]`，但有兩個層級的問題：

1. **wiring.py 硬編碼 `symbols[0]`** — run_id 和 DB metadata 只記錄第一個標的
2. **LiveTrader 與 Backtest 行為不一致** — LiveTrader 每個 symbol 獨立呼叫 `on_bar()`，Backtest 則一次傳入所有 symbols 的 bars 做 cross-sectional 決策

目標：對齊兩者行為，為選股/套利策略打基礎。

## 現狀分析

| 元件 | 多標的支援？ | 說明 |
|------|-------------|------|
| **LiveTrader** | 部分 | `_poll_cycle()` 迭代 symbols，但每個 symbol 各呼叫一次 `on_bar()`，`ctx.bars` 只含單一 symbol |
| **Backtest** | 完整 | 每個 timestamp 呼叫一次 `on_bar()`，`ctx.bars` 包含所有 symbols |
| **Context** | 已準備 | 有 `bars: dict[str, dict]` 和 `symbols: list[str]` 欄位 |
| **LiveExecutor** | 無關 | symbol-agnostic，靠 `Action.symbol` 路由 |
| **現有策略** | 單標的 | 全部只用 `ctx.symbol` + `ctx.bar` |

### Live engine 硬編碼位置

- `librae/live/engine.py:109` — `generate_run_id(..., cfg.symbol, ...)`
- `librae/live/engine.py` (`_register_run`) — `write_run_metadata(..., symbol=self._cfg.symbol, ...)`
- `librae/live/signal_poller.py:56` — `generate_run_id(..., cfg.symbol, ...)`

### backtest engine 同樣問題

- `backtest/engine.py:165` — `generate_run_id(..., self._symbols[0], ...)`
- `backtest/engine.py:278` — `symbol = self._symbols[0]`（用於 `RunMetadata`）

### 現有 bug：跨 symbol action 靜默失敗

`_process_bar()` line 449-450：
```python
get_price=lambda s, action, _bar=bar, _sym=symbol: resolve_fill_price(
    _bar if s == _sym else {}, action, default_fill=self._fill_price),
```

當 action 的 symbol 與當前 bar 的 symbol 不同時，傳入空 dict `{}` → `resolve_fill_price` 回傳 None → `process_actions` 靜默跳過。此 bug 不依賴 watermark 重構，應獨立先修（見 Issue 0）。

### 已知限制：Cost Model 不支援 per-symbol 差異化

`engine.py:451, 511-512`：
```python
get_cost_model=lambda s: self._executor.cost_model  # 忽略 symbol 參數
```

所有 symbol 共用同一個 `CostModel`。`process_actions` 和 `eval_equity` 的 callback 已設計為接受 symbol 參數（`Callable[[str], CostModel]`），但 LiveTrader 端忽略。不在本計劃範圍，作為獨立 issue 追蹤。

---

## Issue 0: 修復跨 symbol action 靜默失敗

**File:** `librae/live/engine.py`

**前置條件**：無，應在 Issue 3 之前獨立完成。

`_process_bar()` 中 `get_price` lambda 對非當前 symbol 的 action 傳入空 dict，導致 action 被靜默跳過。修正方式：從 `self._last_prices` 或 `self._ohlcv_cache` 取得對應 symbol 的 bar 資料。

Issue 3 的 watermark 重構會自然取代此修正（`_process_cross_section` 持有所有 symbol 的 bars），但在 Issue 3 完成前此 bug 仍存在。

## Issue 1: `generate_run_id` 支援多標的

**File:** `librae/core/utils.py`

- 參數型別改為 `str | list[str]`
- 單標的（`str` 或 `len==1` list）：保持原格式 `{strategy}-{symbol}-{tf}-{ts}-{uuid}`
- 多標的（`len>1` list）：改用 `{strategy}-multi{N}-{tf}-{ts}-{uuid}`
- 向後相容，現有傳 `str` 的呼叫者不受影響

## Issue 2: 修復硬編碼 — 統一傳 list

**File:** `librae/live/engine.py`、`librae/live/signal_poller.py`、`librae/backtest/engine.py`

所有呼叫端統一傳 `cfg.symbols`（list）給 `generate_run_id`，由其內部決定格式（見 Issue 1）。DB metadata 的 `symbol` 欄位使用 `",".join(cfg.symbols)`。

- `live/engine.py` `generate_run_id(...)`: 傳 `cfg.symbols`
- `live/engine.py` `_register_run()`: `symbol=",".join(self._symbols)`
- `live/signal_poller.py`: 傳 `cfg.symbols`
- `backtest/engine.py:165`: 傳 `self._symbols`
- `backtest/engine.py:278`: `symbol=",".join(self._symbols)`

## Issue 3: 重構 `_poll_cycle()` 實作水位線對齊 (Watermark Alignment)

**File:** `librae/live/engine.py`

此 issue 範圍較大，拆為 4 個子任務：

### 3a: Watermark 狀態管理

引入可配置的 watermark 策略：

```python
class WatermarkPolicy(str, Enum):
    ANY = "any"    # max(latest_ts) — 任一標的到即觸發（適合獨立信號、單標的）
    ALL = "all"    # min(latest_ts) — 全部到齊才觸發（適合套利/cross-sectional）
```

預設 `ANY`（向後相容單標的），套利策略設定 `ALL`。可透過 `RunConfig.params` 宣告。

引入全域水位線 `last_emitted_ts`（取代現行 per-symbol `last_bar_ts` dict）。

### 3b: `_process_cross_section()` 方法

用新方法 `_process_cross_section(current_ts, all_bars)` 取代既有的 `_process_bar(symbol, ...)`。

目標橫截面流程：
```
Phase 1: for symbol in symbols: fetch latest bar
Phase 2: 依 WatermarkPolicy 計算 watermark_ts
         - ANY: max(latest_ts across symbols)
         - ALL: min(latest_ts across symbols)
Phase 3: 檢查 watermark_ts > last_emitted_ts：
         - 更新 last_emitted_ts = watermark_ts
         - 對所有 symbols 的最新 bars 跑 feature_fn
         - 執行 pending_actions（見 3c）
         - 對所有持倉 symbol 統一 holding_periods += 1
         - 組合 all_bars，建立橫截面 Context
         - 呼叫唯一一次 on_bar(ctx)，產生新的 pending_actions
```

- `ctx.bars` 包含所有 symbols 的最新 bar
- `ctx.symbol` = `symbols[0]`（primary symbol），向後相容單標的策略
- Watermark=ANY 時，遲到的標的使用最近一期的 bar（stale data），不重複觸發 `on_bar()`

### 3c: pending_actions 統一為 single list

現行 `self._pending_actions: dict[str, list[Action]]`（per-symbol dict）改為 `self._pending_actions: list[Action]`（single list），與 Backtest engine 對齊。

```python
# 執行時
prev_actions = self._pending_actions
self._pending_actions = []
# process_actions 已是 symbol-agnostic，靠 action.symbol 路由
result = process_actions(
    prev_actions, self._positions, self._cash, ts,
    get_price=lambda s, action: resolve_fill_price(
        all_bars.get(s, {}), action, default_fill=self._fill_price),
    get_cost_model=self._get_cost_model,
    primary_symbol=self._symbols[0],
)
```

### 3d: feature_fn 多標的支援

現行 feature_fn 簽名為 `(symbol, df) -> df`（per-symbol 呼叫）。套利策略的 feature_fn 需要 cross-sectional 資料（計算 spread），需支援新簽名：

```python
# 單標的（向後相容）
def feature_fn(symbol: str, df: pd.DataFrame) -> pd.DataFrame

# 多標的（新增）
def feature_fn_cross(bars: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]
```

Engine 偵測 strategy 是否提供 `feature_fn_cross`，有則用之，否則 fallback 到 per-symbol 呼叫。

## Issue 4: 對齊 Backtest engine

**File:** `librae/backtest/engine.py`

- Line 165: `generate_run_id(..., self._symbols, ...)` 傳整個 list
- Line 278: `symbol=",".join(self._symbols)`

## Issue 5: 新增測試

**File:** `tests/engine/test_live_runner.py`

1. `test_multi_symbol_cross_sectional_bars` — 兩個 symbol，驗證 `on_bar()` 每 cycle 只呼叫一次，`ctx.bars` 包含兩者
2. `test_multi_symbol_run_id_format` — 驗證 multi-symbol run_id 格式
3. `test_single_symbol_backward_compat` — 確認現有單標的行為不變
4. `test_periods_held_increments_once_per_cycle` — 多標的時 `periods_held` 每 cycle 只 +1（所有持倉 symbol 統一遞增）
5. `test_stale_data_watermark_any` — watermark=ANY 時，驗證 ctx.bars 含 stale bar
6. `test_cross_symbol_action_execution` — 策略產出 Action(symbol="ETH") 在 BTC watermark 觸發時，下一 cycle 正確執行
7. `test_feature_fn_cross_sectional` — 驗證 feature_fn_cross 被正確呼叫
8. `test_watermark_all_timeout` — watermark=ALL 時，一個 symbol 持續延遲不會無限等待

## DB 注意事項

`backtest_runs.symbol` 是 TEXT 欄位，多標的時存逗號分隔字串（e.g., `"BTCUSDT,ETHUSDT"`）。

已知限制：
- 無法用 `WHERE symbol = 'BTCUSDT'` 查詢 → 需要 `string_to_array(symbol, ',')`
- 所有查詢 symbol 的 SQL 使用 `string_to_array(symbol, ',')` 標準化
- 未來若增加「查詢含特定 symbol 的所有 runs」需求，再遷移為 `TEXT[]`

---

## Verification

1. `pytest tests/engine/test_live_runner.py` — 所有現有 + 新增測試通過
2. `pytest tests/` — 全部測試通過
3. 用現有單標的策略（trendpullback）跑一次 sim，確認行為不變
