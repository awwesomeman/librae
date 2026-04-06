# Multi-Symbol LiveTrader Support

> 狀態：planning
> 範圍：engine, live
> 建立日期：2026-04-05
> 最後更新：2026-04-05
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

---

## Issue 1: `generate_run_id` 支援多標的

**File:** `librae/core/utils.py`

- 參數型別改為 `str | list[str]`
- 單標的（`str` 或 `len==1` list）：保持原格式 `{strategy}-{symbol}-{tf}-{ts}-{uuid}`
- 多標的（`len>1` list）：改用 `{strategy}-multi{N}-{tf}-{ts}-{uuid}`
- 向後相容，現有傳 `str` 的呼叫者不受影響

## Issue 2: 修復 Live engine 硬編碼

**File:** `librae/live/engine.py` 與 `librae/live/signal_poller.py`

原本硬編碼 `cfg.symbol`（只取單一標的），需配合設定檔將陣列轉為字串：
- `engine.py:109`: 傳入 `",".join(cfg.symbols)` 給 `generate_run_id` 的 symbol 欄位
- `engine.py` `_register_run()`: 修改 `symbol=",".join(self._symbols)`
- `signal_poller.py:56`: 同樣傳入 `",".join(cfg.symbols)`

## Issue 3: 重構 `_poll_cycle()` 實作水位線對齊 (Watermark Alignment)

**File:** `librae/live/engine.py`

現行流程：
```
for symbol in symbols:
    fetch -> detect new bar -> process_bar(symbol) -> on_bar(ctx with only this symbol)
```

目標橫截面流程 (Watermark)：
```
Phase 1: for symbol in symbols: fetch
Phase 2: 掃描所有 symbols，計算系統最新擁有的時間戳 `watermark_ts = max(latest_ts)`
Phase 3: 檢查 `watermark_ts > last_emitted_ts`：
         - 若為 True：代表系統時間已經跨入場景，更新 `last_emitted_ts = watermark_ts`
         - 對所有 symbols 的最新 bars 跑 feature_fn 
         - 處理當前的 `pending_actions`
         - 組合 `all_bars`，建立橫截面 Context
         - 呼叫 **唯一一次** `on_bar(ctx)`，產生新的 pending actions 
```

關鍵設計：
- **引入全域水位線 (`last_emitted_ts`)**：只要「任意」一個標的產生新 K 棒，就視為該時間點已到達。這保證了 09:00 的 K 線「絕對只會觸發一次 `on_bar`」。
- **靜默吸收遲到資料**：若 BTC 準時達到 09:00，ETH 延遲 5 秒。系統會在 BTC 到達時觸發 09:00 決策（對 ETH 使用 23:55 的舊資料）。當 ETH 的 09:00 資料補到時，因 `watermark_ts` 未增加，不會重複觸發 `on_bar()`，該資料僅寫入 cache / DB。
- `ctx.bars` 包含所有 symbols 的最新 bar。
- `ctx.symbol` = `symbols[0]`（primary symbol），向後相容單標的策略。
- 用新方法 `_process_cross_section(current_ts)` 取代既有的單標的 `_process_bar(symbol, ...)`。

## Issue 4: 對齊 Backtest engine

**File:** `librae/backtest/engine.py`

- Line 165: `generate_run_id(..., self._symbols, ...)` 傳整個 list
- Line 278: `symbol=",".join(self._symbols)`

## Issue 5: 新增測試

**File:** `tests/engine/test_live_runner.py`

1. `test_multi_symbol_cross_sectional_bars` — 兩個 symbol，驗證 `on_bar()` 每 cycle 只呼叫一次，`ctx.bars` 包含兩者
2. `test_multi_symbol_run_id_format` — 驗證 multi-symbol run_id 格式
3. `test_single_symbol_backward_compat` — 確認現有單標的行為不變
4. `test_bars_held_increments_once_per_cycle` — 多標的時 `bars_held` 每 cycle 只 +1

## DB 不需改動

`backtest_runs.symbol` 是 TEXT 欄位，直接存逗號分隔字串即可。

---

## Verification

1. `pytest tests/engine/test_live_runner.py` — 所有現有 + 新增測試通過
2. `pytest tests/` — 全部測試通過
3. 用現有單標的策略（trendpullback）跑一次 sim，確認行為不變
