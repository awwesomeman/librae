# Position Lifecycle：Short + Scaling + Partial Close

> 狀態：proposed
> 範圍：engine, executor
> 建立日期：2026-04-04
> 依據：
> - [2026-04-01 回測引擎優化](../decisions/2026-04-01-backtest-engine-optimization.md) — Short proceeds bug, ctx.cash bug, exit tax bug
> - [enhance_librae_engine](enhance_librae_engine.md) — 整合索引

## 背景

引擎目前每個 symbol 只能持一個 position，且 buy/sell 在已有持倉時被 skip。
需解除：short 修正、同方向加碼、部分平倉。

## 業界最佳實踐（Backtrader / Zipline / QuantConnect / VectorBT）

| 模式 | 說明 | librae 現行 |
|------|------|------------|
| 一 symbol 一 position | 聚合所有 fill 為單一持倉 | ✅ 已符合 |
| 加碼 implicit | buy while long = 自動加碼 | ❌ 被 skip |
| 加權平均入場價 | weighted average；部分平倉不改 avg price | ❌ 不支援 |
| 費用 per-fill | 不嵌入 cost_basis，每次 fill 獨立計算 | ✅ 已符合 |
| Short cash = collateral | 扣全額當保證金 | ✅ 維持不變 |
| TradeResult per close | 每次平倉（含 partial）都產一筆 trade 紀錄 | ❌ 只有 full close |

## 設計決策

| 問題 | 決定 | 理由 |
|------|------|------|
| 加碼怎麼表達 | implicit — buy while long = add | 業界標準，不改 Action API |
| 加碼 sizing | **必須指定 quantity**，不允許 auto-size | 避免 all-in pyramid |
| 部分平倉 | `Action(type="close", quantity=X)` | 複用現有 quantity 欄位 |
| 反向操作（long 中 sell） | reject + log warning | 明確比隱式反轉安全 |
| Short cash 模式 | 維持 collateral（扣全額） | 簡單保守 |
| 費用 pro-rate | 部分平倉按比例分攤累計 entry costs | 業界標準 |
| TradeResult 時機 | **每次 close（含 partial）都產 TradeResult** | 比聚合更簡單、更精確，與 Backtrader 一致 |
| Float drift 防護 | PositionState 存 `total_entry_cost` 而非從 avg_price 反推 | Zipline/QuantConnect 做法 |

### 前置 Bug Fix

> 以下 bug 必須在此次工作中一併修復，否則 short 結果不正確。

**1. Exit tax on short close**（`executor.py` `calc_trade_pnl`）

`is_sell=True` hardcoded → `is_sell=(side == "long")`。
Short close = buy-to-cover，不應課稅。

**2. Short proceeds 公式**（`executor.py` `close_position`）

來源：[decision doc — Short position close proceeds 計算錯誤](../decisions/2026-04-01-backtest-engine-optimization.md)

正確公式：`proceeds = entry_notional + gross_pnl - exit_costs`

**3. Short ctx.cash 偏低**

來源：[decision doc — Short position ctx.cash 偏低](../decisions/2026-04-01-backtest-engine-optimization.md)

collateral 模式下 `estimate_entry_outlay` 對 short 扣全額是設計決策（保守），不是 bug。
但需在文件中明確記錄此行為，讓策略開發者知道 `ctx.cash` 在持有 short 時偏低。

### 注意事項

- **多 symbol 同 bar cash 競爭**：sequential fill model（業界標準），不改
- **Cash < 0 guard**：action loop 中每次 fill 後檢查 `cash >= 0`，不足則 reject fill
- **Live partial fill**：assume-full-fill 模式不變
- **make_fill 費用**：per-fill quantity，已正確（加碼時每次 add 獨立計費）

### 不做的事

- 同 symbol 多個獨立 position
- SL/TP/Trailing Stop（依賴本計畫完成，獨立專案）
- Margin model / borrow cost
- Max position size guard（follow-up）
- FillRecord dataclass（無 consumer，等 fill_log DB 表需求時再加）

---

## 改動

### 1. `librae/core/strategy.py` — PositionState 加欄位

```python
@dataclass
class PositionState:
    ...
    total_entry_cost: float = 0.0  # 累計入場 notional，用於算 avg_price 避免 float drift
```

`entry_price` 改為 derived：`total_entry_cost / (quantity * multiplier)`

### 2. `librae/core/executor.py` — 新增函式 + bug fix

```python
def scale_into_position(pos: PositionState, fill: Fill, cost_model: CostModel) -> None:
    """加碼：更新 total_entry_cost、累加 quantity 和 entry costs。"""

def reduce_position(pos: PositionState, closed_qty: float) -> None:
    """部分平倉後縮小持倉：按比例減少 entry costs 和 total_entry_cost。"""
```

修改 `close_position`：
- 加 `*, quantity: float | None = None`（keyword-only）
- 回傳 `tuple[TradePnL, float, bool]`（pnl, proceeds, fully_closed）
- 修 exit tax：`is_sell=(side == "long")`
- 修 short proceeds：`entry_notional + gross_pnl - exit_costs`

### 3. `librae/core/executor.py` — 抽出共用 action 處理函式

```python
def process_actions(
    actions: list[Action],
    positions: dict[str, PositionState],
    cash: float,
    get_price: Callable[[str], float],
    get_cost_model: Callable[[str], CostModel],
    ts: datetime,
    primary_symbol: str,
) -> ActionResults:
    """共用 action 處理邏輯，backtest 和 live engine 都呼叫。"""
```

避免 backtest/live 重複實作 ~50 行相同邏輯。

### 4. `librae/backtest/engine.py` — 改用 process_actions

Action loop 替換為呼叫 `process_actions()`。

### 5. `librae/live/engine.py` — 改用 process_actions

`_process_bar` 中的 action 處理邏輯替換為呼叫 `process_actions()`。

### 6. 不改的檔案

| 檔案 | 理由 |
|------|------|
| `cost_model.py` | 已 direction-agnostic |
| `_eval_equity` | 已正確處理 long/short MTM |

---

## 測試（`tests/engine/test_position_scaling.py`）

### Executor 單元測試

| # | 測試 |
|---|------|
| 1 | scale_into_position 更新 avg price（透過 total_entry_cost） |
| 2 | scale_into_position 累計 costs |
| 3 | reduce_position 按比例減少 costs |
| 4 | partial close PnL 正確 |
| 5 | partial close quantity = total qty → fully_closed = True |

### Short 測試

| # | 測試 |
|---|------|
| 6 | short open + close 獲利 |
| 7 | short open + close 虧損 |
| 8 | short 零成本 round-trip → cash 不變 |
| 9 | short close tax = 0（buy-to-cover 不課稅） |

### Scaling 整合測試

| # | 測試 |
|---|------|
| 10 | long 加碼 → avg price 正確 → close PnL 正確 |
| 11 | 多次加碼（buy 5, buy 3, buy 2）→ avg price = weighted mean |
| 12 | 加碼後部分平倉 → 剩餘持倉正確 → close 剩餘 |
| 13 | 兩次 partial close + full close → 累計 PnL 正確（無 penny leak） |
| 14 | short 加碼 + short partial close + short full close |

### Edge Cases

| # | 測試 |
|---|------|
| 15 | 加碼未指定 quantity → rejected |
| 16 | buy while short → rejected |
| 17 | 部分平倉 quantity > 持倉量 → clamp |
| 18 | Action(type="close", quantity=0) → rejected |
| 19 | 極小 quantity（1e-8）→ PnL 和 costs 非負 |
| 20 | 加碼後 cash < 0 → fill rejected |
| 21 | 多 symbol：scale A 不影響 B |
| 22 | scaled position equity curve MTM 正確 |
| 23 | force-close scaled position → PnL 用 avg price |
| 24 | 現有 long-only 策略結果不變（regression） |

---

## 實作順序

1. Bug fix：exit tax、short proceeds（`executor.py`）
2. PositionState 加 `total_entry_cost`
3. 新增 `scale_into_position`、`reduce_position`、修改 `close_position`
4. Executor 單元測試 #1-5
5. 抽出 `process_actions` 共用函式
6. Backtest engine 改用 `process_actions`
7. Backtest 整合測試 #6-24
8. Live engine 改用 `process_actions`
9. 全部 tests regression
