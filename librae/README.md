# librae

量化回測與即時交易引擎。提供策略執行、持倉管理、成本模擬、績效計算的完整框架。

**回測、模擬、實盤共用同一份策略，零修改。**

---

## 架構

```
core/                       共用 domain model（純計算，無 I/O）
├── strategy.py             BaseStrategy, Action, Context, Position, PositionState, Fill
├── executor.py             make_fill, calc_trade_pnl, close_position, TradeResult, TradePnL
├── cost_model.py           CostModel（手續費 / 滑價 / 稅 / 合約乘數）
├── metrics.py              compute_all（QuantStats adapter）
└── utils.py                generate_run_id, infer_timeframe, to_ccxt, to_canonical

backtest/                   回測 runtime
├── engine.py               Backtest — bar-by-bar 執行 + build_output()
├── schema.py               BacktestOutput, RunMetadata, StrategyMetrics, TradeRecord
└── persistence.py          save_output / load_output（JSON + CSV + Parquet）

live/                       即時 / 模擬 runtime
├── engine.py               LiveTrader — polling loop + 信號偵測
├── executor.py             LiveExecutor（sim 通知 / live 下單）
└── wiring.py               build_live_trader() — DB + Telegram + heartbeat 組裝

config/                     markets.yaml（市場參數）
notifications/              Telegram 推播
cli.py                      共用 CLI parser + config YAML 載入
```

### 依賴方向

```
backtest/ ──→ core/
live/     ──→ core/
```

`backtest/` 和 `live/` 之間無直接依賴，共用邏輯全部在 `core/`。

### 資料流

```
策略 ETL (utils.py)  →  DataFrame (MultiIndex + 信號欄位)
                              ↓
策略邏輯 (strategy.py)  →  on_bar(ctx) → list[Action]
                              ↓
引擎 (engine.py)     →  make_fill / close_position → Fill / TradeResult
                              ↓
輸出 (build_output)  →  BacktestOutput (metrics + equity_curve + trades)
```

---

## 使用方式

### 回測 (backtest)

```python
from librae import Backtest, BaseStrategy, Action, Context
from librae.backtest.persistence import save_output
from librae.config import get_market

# 1. 定義策略
class MyStrategy(BaseStrategy):
    def on_bar(self, ctx: Context) -> list[Action]:
        if ctx.positions.get(ctx.symbol):
            if ctx.bar.get("exit_signal"):
                return [Action(type="close", symbol=ctx.symbol)]
            return []
        if ctx.bar.get("entry_signal"):
            return [Action(type="buy", symbol=ctx.symbol)]
        return []

# 2. 跑引擎
df = fetch_and_prepare(symbol, months)          # 你的 ETL
bt = Backtest(data=df, strategy=MyStrategy(), market_config=get_market("crypto"))
bt.add_benchmark(df.xs(symbol, level="symbol")["close"])
bt.run()

# 3. 取得結果
output = bt.build_output(annualize=True)        # BacktestOutput
save_output(output, Path("data/backtests"))     # JSON + CSV
```

**資料格式**：MultiIndex DataFrame `(symbol, datetime)` + OHLCV + 自訂特徵欄位。

### 模擬監控 (sim)

```python
from librae.live.wiring import build_live_trader

trader = build_live_trader(
    strategy=MyStrategy(),
    strategy_name="my_strategy",
    feature_fn=prepare_signals,     # 同一個 ETL pipeline
    symbols=["BTCUSDT"],
    timeframe="H1",                 # canonical label，內部用 to_ccxt() 轉換
    poll_interval=60,
)
trader.run()  # DB 寫入、Telegram、heartbeat、KPI 更新全由引擎處理
```

### 模式對比

| | 回測 (backtest) | 模擬 (sim) | 實盤 (live) |
|---|---|---|---|
| 資料來源 | 歷史 OHLCV | 即時 OHLCV（polling） | 即時 OHLCV |
| 執行器 | `core.make_fill()` | `LiveExecutor(simulation=True)` | `LiveExecutor(simulation=False)` |
| 下單 | 模擬成交 | 模擬成交 + Telegram 通知 | 真實下單（Phase 4） |

---

## 核心類型

### Strategy 層

| 類型 | 說明 |
|------|------|
| `BaseStrategy` | 抽象基類，實作 `on_bar(ctx) -> list[Action]` |
| `Context` | 不可變快照：bar data + positions + cash + bar_index |
| `Action` | 策略意圖：`type` = buy / sell / close / hold |
| `Position` | 凍結持倉（給策略看）：symbol, side, entry_price, quantity, unrealized_pnl |
| `PositionState` | 可變持倉（引擎內部）：追蹤 bars_held, entry_commission, entry_slippage |

### Execution 層

| 類型 | 說明 |
|------|------|
| `Fill` | 成交回報：price, quantity, commission, slippage, tax |
| `TradeResult` | 完成交易：entry/exit 全資訊 + PnL + holding_bars |
| `TradePnL` | PnL 拆解：gross_pnl, net_pnl, commission, slippage, tax |
| `CostModel` | 成本模型（frozen）：multiplier, commission_rate, slippage_ticks, tick_size, tax |

### Output 層

| 類型 | 說明 |
|------|------|
| `BacktestOutput` | 頂層容器（frozen）：run_metadata + equity_curve + trades + metrics |
| `RunMetadata` | run_id, strategy, symbol, timeframe, start/end/run timestamps |
| `StrategyMetrics` | 績效指標：total_return, sharpe, sortino, calmar, max_drawdown, win_rate... |
| `TradeRecord` | 交易紀錄（含 unit fields 支援多市場）|
| `EquityCurvePoint` | 單點：ts, equity, ret_1d, drawdown, benchmark_equity |

### 共用函數

| 函數 | 說明 |
|------|------|
| `make_fill(action, price, cash, cost_model)` | 模擬成交（backtest 直接用） |
| `close_position(pos, exit_price, cost_model)` | 平倉 PnL + proceeds（backtest + live 共用） |
| `calc_trade_pnl(...)` | 單筆交易 PnL 拆解 |
| `compute_all(equity_values, timestamps, trade_pnls, ...)` | 績效計算（QuantStats adapter） |
| `direction(side)` | `"long"` → +1.0, `"short"` → -1.0 |

---

## 設計決策

- **Primitive signature**: `compute_all()` 接受 `Sequence[float]` / `Sequence[datetime]`，不依賴 `BacktestResult`，讓 live 引擎也能直接呼叫。
- **Lazy import**: `quantstats` 在 `compute_all()` 內延遲載入，`import librae` 保持 <1s。
- **PositionState in core**: backtest 和 live 共用同一個可變持倉型別，消除重複的 PnL / bars_held 邏輯。
- **Pre-computed bars**: `_precompute_bars()` 一次性將 DataFrame 轉為 dict-of-dicts，避免 hot loop 中每 bar 呼叫 `to_dict()`。
- **Frozen dataclasses**: `BacktestOutput`, `StrategyMetrics`, `TradeRecord`, `CostModel` 等皆為 frozen，確保不可變。
