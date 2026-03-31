# 2026-03-27 — Strategy Protocol + Executor 分離

> 狀態：implementing

## 背景

v1 回測引擎重構（engine.py + cost_model.py + metrics.py）完成後，發現核心架構問題：
1. `signal_engine/generate_signals()` 和 `engine.py` 各自追蹤持倉狀態（dual state machine → desync 風險）
2. engine 綁死單資產、long-only、全倉進出
3. 回測和實盤無法共用策略程式碼
4. `run_backtest.py` script 硬寫 TrendPullback，無法擴展

## 決策

### 三層解耦

```
ETL          → df（含信號欄位）         使用者自由決定怎麼算
Strategy     → on_bar(ctx) → Action[]   看 ctx.bar 欄位做決策
Engine       → Executor.execute(action)  管持倉、算 PnL
```

### 資料格式統一

所有資料統一用 MultiIndex `(instrument: str, datetime: Timestamp)`。
單資產是 `instruments=["BTCUSDT"]` 的特例。
Engine 用 `groupby('datetime')` 預分組，loop 中 `get_group()` O(1) 查詢。

### Strategy 介面

```python
class BaseStrategy(ABC):
    @abstractmethod
    def on_bar(self, ctx: Context) -> list[Action]: ...
```

- 策略不追蹤持倉，看 `ctx.positions`（engine 擁有）
- 策略不準備資料，看 `ctx.bar` 裡的欄位（ETL 層準備好）
- 策略不知道自己在回測還是實盤

### Executor 分離

```python
class Executor(Protocol):
    def execute(self, action: Action, price: float, cash: float) -> Fill | None: ...
```

| Executor | 用途 |
|----------|------|
| `BacktestExecutor(cost_model)` | 回測：CostModel 模擬成交 |
| `LiveExecutor(broker, simulation)` | 實盤：simulation=True 只出訊號，False 真下單（Phase 2-3） |

### Backtest 入口

```python
bt = Backtest(
    data=df,                                    # MultiIndex，自帶 instrument
    strategy=TrendPullback({"max_hold_bars": 24}),
    initial_balance=100_000,
)
result = bt.run()
```

- 自動從 MultiIndex 取 instruments
- 自動從 markets.yaml 建 CostModel per instrument
- 使用者不需要手動建 CostModel 或 Executor

### CostModel 自動解析

markets.yaml 的 `symbol` 欄位對應 MultiIndex 的 instrument level：
```
df 裡的 "BTCUSDT" → markets.yaml BTC_USDT.symbol == "BTCUSDT" → CostModel
```
找不到 → 零成本 default（研究階段不想管成本時）。

### 使用方式

```python
# 1. ETL（使用者自己準備）
df["entry_signal"] = compute_entry_conditions(df)
df["exit_signal"] = compute_exit_conditions(df)

# 2. 回測
bt = Backtest(data=df, strategy=MyStrategy(params), initial_balance=100_000)
result = bt.run()

# 3. 實盤（同一個 strategy，Phase 2-3）
live = Live(strategy=MyStrategy(params), broker=CCXTBroker(exchange), simulation=True)
live.start()
```

## 實作範圍

### 新建

| 檔案 | 內容 |
|------|------|
| `quant_lab/backtest/strategy.py` | Context, Position, Action, Fill, BaseStrategy ABC |
| `quant_lab/backtest/executor.py` | Executor protocol, BacktestExecutor |

### 修改

| 檔案 | 改動 |
|------|------|
| `engine.py` | `run_backtest()` → `Backtest` class，MultiIndex groupby loop，多資產 positions dict |
| `signal_engine/trendpullback.py` | `generate_signals()` 拆成 `compute_entry_conditions()` + `compute_exit_conditions()`，純布林 Series |
| `runners.py` | `make_backtest_fn()` 改用新介面 |
| `__init__.py` | export 新 types |

### 不動

- `cost_model.py`、`metrics.py`、`schema.py`、`persistence.py`、`market_config.py`

### 先不做（Phase 2-3）

- `LiveExecutor`、`LiveRunner`
- Broker wrappers（CCXT/IB/Shioaji）
- Telegram notifier 整合
