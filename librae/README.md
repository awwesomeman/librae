# librae

量化回測與即時交易引擎。提供策略執行、持倉管理、成本模擬、績效計算的完整框架。

**回測、模擬、實盤共用同一份策略，零修改。**

---

## 架構

```
core/                       共用 domain model（純計算，無 I/O）
├── strategy.py             BaseStrategy, Action, Context, Position, PositionState, Fill
├── executor.py             make_fill, process_actions, calc_trade_pnl, close_position, scale_into_position, reduce_position, liquidate_all
├── cost_model.py           CostModel（手續費 / 滑價 / 稅 / 合約乘數 / 保證金）
├── metrics.py              compute_all（QuantStats adapter）
├── run_config.py           RunConfig — 統一執行參數（frozen dataclass）
└── utils.py                generate_run_id, infer_timeframe, to_ccxt, to_canonical

backtest/                   回測 runtime
├── engine.py               Backtest — bar-by-bar 執行 + build_output()
├── schema.py               BacktestOutput, RunMetadata, StrategyMetrics, OrderEventRecord
└── charts.py               plot_trades — lightweight-charts 疊 order_events 進出場點（純渲染，不重算，本地研究用；[extra: viz]）

live/                       即時 / 模擬 runtime
├── engine.py               LiveTrader — polling loop + 信號偵測
└── executor.py             LiveExecutor（sim 通知 / live 下單）

config/                     設定管理
├── markets.yaml            市場參數（成本模型、tick_size、乘數、保證金率）
├── market_config.py        MarketConfig dataclass + load helpers
└── notification.py         TelegramConfig + NotificationConfig dataclass

notifications/              Telegram 推播
├── telegram.py             TelegramAdapter + TelegramCredentials

cli.py                      共用 CLI parser + config YAML 合併
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
from librae import Backtest, BaseStrategy, Action, Context, RunConfig

# 1. 定義策略
class MyStrategy(BaseStrategy):
    def on_bar(self, ctx: Context) -> list[Action]:
        if ctx.positions.get(ctx.symbol):
            if ctx.bar.get("exit_signal"):
                return [Action(type="close", symbol=ctx.symbol)]
            return []
        if ctx.bar.get("entry_signal"):
            return [Action(type="long", symbol=ctx.symbol)]
        return []

# 2. 跑引擎（通常由 cli.build_config() 建立 RunConfig）
df = fetch_and_prepare(symbol, months)          # 你的 ETL
bt = Backtest(data=df, strategy=MyStrategy(), cfg=cfg)
bt.add_benchmark(df.xs(symbol, level="symbol")["close"])
bt.run()

# 3. 取得結果
output = bt.build_output()                      # BacktestOutput
```

**資料格式**：MultiIndex DataFrame `(symbol, datetime)` + OHLCV + 自訂特徵欄位。

### 多資產 / 選股策略

引擎本身是 portfolio-level 設計（`positions` 是 `dict[symbol]`，`equity_curve`/`metrics` 皆為組合層級），`on_bar()` 可在同一根 bar 回傳多個不同 symbol 的 `Action`，不需改動 engine/executor/schema。唯一要注意：`Action.quantity=None` 預設用光可用現金（單資產便利預設），同一 bar 開多檔部位時必須自行算好每檔 `quantity`（見 `strategy.py` 中 `Action.quantity` docstring），否則第一個 Action 會吃光現金。

### 本地看進出場點 (trade chart)

`pip install -e ".[viz]"` 後使用。純渲染 `build_output()` 已算好的 `order_events`，不重新模擬/計算，數字保證跟 `strategy_performance`/Grafana 一致（SSOT 見上方「多資產 / 選股策略」段落）。

```python
from librae.backtest.charts import plot_trades, plot_trades_by_run_id

ohlcv = df.xs(symbol, level="symbol")            # 單一 symbol 的 OHLCV
plot_trades(ohlcv, output.order_events, symbol)  # 剛跑完回測，手上已有 output

plot_trades_by_run_id(run_id)                    # 或者：不重跑回測，直接從 DB 讀已落地的 run
```

`plot_trades_by_run_id` 讀的是 `db.timescale_reader.load_trade_events`/`load_ohlcv`——跟 Grafana 同一份 `trade_events`/`ohlcv` 表，同源保證不 drift。

### 風控 (risk controls)

引擎層級強制，策略無法繞過；三者皆預設關閉（`None`）。backtest/live 共用同一份 `core.executor.liquidate_all`/`_cap_fill_to_notional`/`_cap_fill_to_volume`。

```python
cfg = RunConfig(..., params={
    "max_position_pct": 0.3,             # 單一部位 notional 上限 = 30% 最新已知權益
    "max_drawdown_pct": 0.2,             # 權益從高點回落 20% -> 全平倉並永久停止進場
    "max_volume_participation_pct": 0.1, # 單筆成交量上限 = 10% 該根 bar 的 volume
})
```

- `max_position_pct`：新倉/加碼皆會被裁量（裁量後重算 commission/slippage/tax），不是直接拒絕。
- `max_drawdown_pct`：觸發後呼叫 `liquidate_all()` 全平倉，並停止呼叫策略 `on_bar()`（live 仍持續 polling/監控，只是不再進場）；一次觸發即永久生效，需重啟該次 run。
- `max_volume_participation_pct`：只限制單筆成交（新倉/加碼），不是累加 vs 部位大小；跟 `max_position_pct` 一樣是裁量不拒絕。只作用於進場 —— 出場（策略平倉、停損/停利、force close、回撤熔斷平倉）不受此限制。
- 成交量感知的滑價（`CostModel.impact_coef`）跟這個開關無關、預設也是關閉：只要有成交量資料傳入，且該市場/symbol 的 `impact_coef > 0`（在 `markets.yaml`/`symbols.yaml`/`cost_overrides` 設定），滑價就會隨單筆成交佔該 bar volume 的比例線性放大，無論有沒有設定上限。

### 模擬監控 (sim)

```python
from librae.live.engine import LiveTrader

trader = LiveTrader(
    strategy=MyStrategy(),
    feature_fn=prepare_signals,     # 同一個 ETL pipeline
    cfg=cfg,                        # RunConfig（由 cli.build_config() 建立）
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
| `Context` | 不可變快照：ts, symbol, symbols, bar, bars, positions, cash, period_index |
| `Action` | 策略意圖：`type` = long / short / close / hold |
| `Position` | 凍結持倉（給策略看）：symbol, side, entry_price, quantity, unrealized_pnl |
| `PositionState` | 可變持倉（引擎內部）：追蹤 periods_held, entry_commission, entry_slippage, entry_tax, total_entry_cost |

### Execution 層

| 類型 | 說明 |
|------|------|
| `Fill` | 成交回報：price, quantity, commission, slippage, tax |
| `TradeResult` | 完成交易：entry/exit 全資訊 + PnL + periods_held |
| `TradePnL` | PnL 拆解：gross_pnl, net_pnl, commission, slippage, tax |
| `CostModel` | 成本模型（frozen）：multiplier, commission_rate, slippage_ticks, tick_size, tax, long/short_margin_rate, impact_coef（成交量衝擊係數，預設 0 關閉） |

### Output 層

| 類型 | 說明 |
|------|------|
| `BacktestOutput` | 頂層容器（frozen）：run_metadata + equity_curve + trades + metrics |
| `RunMetadata` | run_id, strategy, symbol, timeframe, start/end/run timestamps |
| `StrategyMetrics` | 績效指標：total_return, sharpe, sortino, calmar, max_drawdown, win_rate... |
| `OrderEventRecord` | 部位生命週期事件（open/add/reduce/close）|
| `EquityCurvePoint` | 單點：ts, equity, period_return, drawdown, benchmark_equity |

### 共用函數

| 函數 | 說明 |
|------|------|
| `make_fill(action, price, cash, cost_model)` | 模擬成交（backtest 直接用） |
| `process_actions(actions, ...)` | 共用 action 迴圈（backtest + live 共用） |
| `close_position(pos, exit_price, cost_model)` | 平倉 PnL + proceeds |
| `liquidate_all(positions, bars, ts, ...)` | 全平倉（end-of-run / 最大回撤熔斷共用） |
| `scale_into_position(pos, fill, cost_model)` | 同方向加碼（weighted avg entry） |
| `reduce_position(pos, quantity, exit_price, cost_model)` | 部分平倉 |
| `calc_trade_pnl(...)` | 單筆交易 PnL 拆解 |
| `compute_all(equity_values, timestamps, trade_pnls, ...)` | 績效計算（QuantStats adapter） |
| `direction(side)` | `"long"` → +1.0, `"short"` → -1.0 |

---

## 設計決策

- **Primitive signature**: `compute_all()` 接受 `Sequence[float]` / `Sequence[datetime]`，不依賴 `BacktestResult`，讓 live 引擎也能直接呼叫。
- **Lazy import**: `quantstats` 在 `compute_all()` 內延遲載入，`import librae` 保持 <1s。
- **PositionState in core**: backtest 和 live 共用同一個可變持倉型別，追蹤 `total_entry_cost` 避免 scaling 時浮點數漂移。
- **Pre-computed bars**: `_precompute_bars()` 一次性將 DataFrame 轉為 dict-of-dicts，避免 hot loop 中每 bar 呼叫 `to_dict()`。
- **Frozen dataclasses**: `BacktestOutput`, `StrategyMetrics`, `OrderEventRecord`, `CostModel` 等皆為 frozen，確保不可變。
- **Margin rate 統一公式**: `margin_rate` = 從可用現金流出的比例 / notional。開倉 `cash -= notional * margin_rate + costs`，平倉 `proceeds = notional * margin_rate + gross_pnl - exit_costs`，equity `mtm += unrealized + notional * margin_rate`。一個公式覆蓋現貨（1.0）、美股做空（0.5, Reg T）、台股融券（0.9）、期貨（0.067）。使用者可透過 `cost_overrides` 覆蓋預設值。

---

## Config API

> 設定檔的完整說明（環境變數清單、YAML 範例、CLI 參數表）見 [根目錄 README — 設定檔總覽](../README.md#設定檔總覽)。
> 以下僅說明引擎內部的程式碼調用方式。

### MarketConfig（市場成本）

來源：`librae/config/markets.yaml`（程式啟動時讀取）

```python
from librae.config.market_config import get_market
from librae.core.cost_model import CostModel

market = get_market("crypto")            # → MarketConfig (frozen dataclass)
cost_model = CostModel.from_market(market)
```

### TelegramAdapter（通知）

來源：行為設定從 `strategies/*/config.yaml` 的 `telegram:` 區塊，secrets 從環境變數。

```python
from librae.config.notification import TelegramConfig
from librae.notifications.telegram import TelegramAdapter, TelegramCredentials

config = TelegramConfig.from_dict(yaml_dict.get("telegram", {}))
creds = TelegramCredentials.from_env("TELEGRAM")
adapter = TelegramAdapter(config=config, credentials=creds)
```

`TelegramAdapter` 方法與對應 flag（定義在 `librae/config/notification.py`）：

| 方法 | Flag | 預設 |
|------|------|------|
| `send_signal()` | `notifications.signal` | `True` |
| `send_startup()` / `send_shutdown()` | `notifications.startup` | `True` |
| `send_alert()` | `notifications.error` | `True` |
| `send_status()` | `notifications.status.enabled` | `False` |

### parse_with_config（CLI + YAML 合併）

策略 `run.py` 用這組函數處理 CLI 參數和 config.yaml 合併。
`telegram` 等巢狀區塊自動分離為 dict，不經過 argparse。

```python
from librae.cli import base_parser, parse_with_config, setup_logging

p = base_parser("My strategy")
args = parse_with_config(p, config_path=Path(__file__).parent / "config.yaml")
# args.mode, args.dry_run      ← runtime flags (argparse)
# args.strategy                ← dict from config.yaml strategy: block
# args.telegram                ← dict from config.yaml telegram: block
```
