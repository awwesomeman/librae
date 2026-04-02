# 2026-03-27 — 回測引擎重構：統一模組化 + 事件型架構

> 狀態：implemented
> 注記：librae backtest engine 已全面落地，舊 Lumibot POC script 已移除
> ⚠️ 引擎上線後發現的 bug 與功能缺口（short proceeds、SL/TP）見 [04-01 回測引擎優化](2026-04-01-backtest-engine-optimization.md)

## 背景

Phase 0 的回測引擎以 POC 形式完成，邏輯散落在 3 個檔案：
- `scripts/run_backtest_lumibot_btc.py:_run_vectorised_backtest()` (~170 行)
- `quant_lab/strategies/trendpullback_btc.py:backtest()` (~120 行)
- `quant_lab/strategies/trendpullback_mxfr1.py:run_trendpullback_backtest()` (~77 行)

每份各自實作 bar-by-bar loop、成本計算、PnL、metrics，導致：
- 新增策略必須重寫整個 backtest loop
- 成本模型不一致（bps vs InstrumentConfig 混用）
- PnL 只有現貨邏輯，期貨的 tick_value 沒用到
- `metrics.py` 有完整 registry 但 script 自己重算 Sharpe/MDD
- `runners.py` 的 walk-forward / stability 框架因缺少 reference engine 而無法使用

## 決策

### 1. 建立統一回測引擎 `quant_lab/backtest/engine.py`

**設計原則（借鑑 Backtrader / zipline / QSTrader / bt 研究）：**

| 原則 | 來源 | 說明 |
|------|------|------|
| Engine 只做交易執行 | Backtrader Cerebro | 不算 metrics、不做 I/O、不做資料對齊 |
| 單一 DataFrame 輸入 | zipline data feed | 多頻率在外部 merge 好，engine 只看一個時間軸 |
| `multiplier` 統一 PnL | Backtrader `cashadjust()` | 現貨 mult=1，期貨 mult=tick_value/tick_size，engine 零 if/else |
| commission + tax 分離 | QSTrader FeeModel | 台股證交稅、期交稅 vs crypto 無稅 |
| 極簡實作 | bt (460 行=完整框架) | engine + cost_model < 300 行 |

**接口：**
```python
def run_backtest(
    df: pd.DataFrame,       # 單一 DataFrame，含所有 features + signal
    signal_fn: Callable,    # 吃 df 回傳 signal column
    cost_model: CostModel,  # 統一成本介面
    budget: float = 100_000,
) -> BacktestResult
```

### 2. `CostModel` 統一成本/PnL

```python
@dataclass(frozen=True)
class CostModel:
    multiplier: float        # 現貨=1.0, 期貨=tick_value/tick_size
    commission_rate: float
    min_commission: float
    slippage_ticks: float
    tick_size: float
    transaction_tax: float   # 賣出時課

    def calc_pnl(entry, exit, qty) -> float  # 統一公式
    def total_cost(price, qty, is_sell) -> float  # commission + slippage + tax
```

### 3. metrics.py 改用 QuantStats

自建 registry（10+ @register_metric）全部用 QuantStats 取代。
metrics.py 變成 thin adapter：吃 equity series → 呼叫 QuantStats → 回傳 StrategyMetrics。
只有 QuantStats 沒有的指標（exposure_ratio, avg_hold_bars）才自己算。

### 4. Lumibot 降為 optional

Lumibot 只用於 live trading adapter，不再參與回測。
未來 live trading 考慮直接包 CCXT / ib_insync / Shioaji，不經過 Lumibot 層。

## 明確不做（避免過度設計）

| 項目 | 理由 |
|------|------|
| Limit/Stop order 撮合 | 策略是 H1 close 進出，不需 intra-bar 撮合 |
| Generator pattern 資料源 | H1 六個月 4300 bars，M5 52K bars，Python loop < 2s |
| Portfolio-level 風控 | 單資產階段 |
| Broker state machine | 等 live trading |
| Numba/@njit 加速 | 效能門檻：tick level (百萬+ bars) 才需要 |

## 影響範圍

| 檔案 | 動作 |
|------|------|
| `quant_lab/backtest/engine.py` | 新建 |
| `quant_lab/backtest/cost_model.py` | 新建 |
| `quant_lab/backtest/metrics.py` | 重構為 QuantStats adapter |
| `scripts/run_backtest_lumibot_btc.py` | 重構，瘦身 ~500→~150 行 |
| `quant_lab/strategies/trendpullback_btc.py` | 移除 inline backtest |
| `quant_lab/strategies/trendpullback_mxfr1.py` | 移除 inline backtest |
| `quant_lab/backtest/runners.py` | 加 `make_backtest_fn()` factory |
