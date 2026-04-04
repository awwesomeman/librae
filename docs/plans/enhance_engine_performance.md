# Engine Performance 微優化

> 狀態：implemented
> 範圍：engine, strategy, data
> 建立日期：2026-04-04
> 依據：
> - [2026-04-01 回測引擎優化](../decisions/2026-04-01-backtest-engine-optimization.md) — #3 slots, #7 cache trim
> - [enhance_librae_engine](enhance_librae_engine.md) — 整合索引

## 背景

從 decision doc 挑出低風險、低工作量的改善項目，可獨立於 Position Lifecycle 計畫實施。

> 注意：原 decision doc #1（Position snapshot reuse）經 review 後移除。
> `Position` 是 `frozen=True` dataclass，`unrealized_pnl` 每 bar 都變，
> 無法有效 reuse snapshot。等 benchmark 基線建立後再評估替代方案。

---

## 改動

### 1. Context `__slots__`（~10% 效能提升）

來源：[decision doc #3](../decisions/2026-04-01-backtest-engine-optimization.md)

**位置**：`librae/core/strategy.py` Context dataclass

```python
@dataclass(frozen=True, slots=True)
class Context:
    ...
```

Python 3.10+ 支援。1 行改動，零風險。

### 2. Cache trim date range（look-ahead bias 修復）

來源：[decision doc #7](../decisions/2026-04-01-backtest-engine-optimization.md)

**位置**：`data/binance.py` `fetch_ohlcv()`

**問題**：cache 返回所有歷史資料，不依 `start`/`end` 截斷。
策略可能意外存取未來資料 — **這是 look-ahead bias 風險，不只是效能問題**。

**改法**：返回前加 DataFrame filter：
```python
if start_dt:
    df = df[df["timestamp"] >= start_dt]
if end_dt:
    df = df[df["timestamp"] <= end_dt]
```

---

## 測試

| # | 測試 |
|---|------|
| 1 | Context 有 `__slots__`（`hasattr(Context, '__slots__')` 為 True） |
| 2 | `fetch_ohlcv` 帶 end 參數時不返回超出範圍的資料 |
| 3 | `fetch_ohlcv` 帶 start 參數時不返回之前的資料 |
| 4 | 現有 tests 全過（regression） |

## 實作順序

1. Cache trim（優先 — 修正 look-ahead bias）
2. Context `__slots__`
3. 跑全部 tests
