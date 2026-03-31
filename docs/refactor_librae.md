# Librae Framework Refactoring

## Context

Three problems with the current design:

1. **Boilerplate in run.py**: Every strategy repeats a 6-step pipeline (`bt.run()` → `compute_all()` → `build_backtest_output()` → `save_backtest_output()` → `write_backtest_output()`), manually extracting `start_ts`/`end_ts` and passing intermediate objects.

2. **Fragmented file structure**: Output-related logic is scattered across 5 small files (schema.py, utils.py, metrics.py, persistence.py, contracts.py — ~805 lines total). Understanding "how an output is produced" requires jumping across all of them.

3. **Flat structure without domain boundaries**: backtest 和 live/sim 混在同一層，共用的 domain model 沒有明確分離，依賴方向不清楚。同樣的計算邏輯（trade PnL, drawdown）在 backtest 和 live 各寫一份，live 那份還有 bug（缺 tax、commission 不精確）。

## Design Principles

1. **`BacktestResult` = engine internal, `BacktestOutput` = user-facing** — clear naming boundary
2. **Benchmark stays on `self`, not in result** — it's analysis config, not trade facts
3. **Persistence 與 domain model 分離** — `BacktestOutput` 只定義資料結構，序列化/DB 寫入由獨立模組負責（SRP）
4. **Consolidate files** — merge small single-purpose files into cohesive modules
5. **Engine 只負責執行回測 + 轉接基礎設施** — 策略研究/穩健性檢測不是 engine 的職責
6. **Package 結構反映 runtime 邊界** — `core/` (共用) / `backtest/` / `live/` 三層分離
7. **共用計算邏輯集中管理** — trade PnL 在 `core/executor.py`，metrics 在 `core/metrics.py`，避免重複實作

### Python Coding Standards 對照（參照 python/coding-standards skill）

| 原則 | 本次如何遵循 |
|------|-------------|
| **SRP** | `build_output()` 是 thin facade，只做編排（調用 `compute_all` + 組裝 output），不包含計算邏輯。Persistence 不放在 dataclass 上，由獨立函式負責。 |
| **DI** | `write_output_db(output, dsn)` 透過參數傳入 DSN。`load_output(path)` 透過參數傳入路徑。domain model 不依賴 infrastructure。 |
| **DRY / Rule of Three** | 6-step pipeline 已在 2 個 run.py 重複，封裝為 `build_output()`。Trade PnL 計算統一在 `core/executor.py`，metrics 統一在 `core/metrics.py`。 |
| **YAGNI** | 不預先加入 Tidal 的 metrics gap（alpha/beta/drawdown analysis），指標缺口另案處理。不加 `trading_days_per_year` 到 MarketConfig，現有 `_infer_annual_periods` 已夠用。 |
| **型別標註** | 所有新增方法完整標註參數 + 回傳型別，不使用 `Any`。 |
| **# WHY 註解** | 合併檔案時保留原有 `# WHY:` 註解；新增的設計決策加上動機說明。 |
| **命名** | 函式用動詞-名詞（`build_output`, `generate_run_id`），私有成員用 `_` 前綴。Domain-specific class name（`Backtest`, `LiveTrader`），不用 generic name。 |

## Part A: Package 結構重組 + File Consolidation + Runner 清理

### 重構後完整結構

```
librae/
├── __init__.py              re-export 常用 API（Backtest, BaseStrategy, ...）
│
├── core/                    backtest + live 共用的 domain model
│   ├── __init__.py
│   ├── strategy.py          BaseStrategy, Action, Context, Position, Fill
│   ├── cost_model.py        CostModel
│   ├── executor.py          make_fill, calc_trade_pnl（平倉計算，執行邏輯）
│   ├── metrics.py           compute_all（給定 equity curve + trades → 指標，共用）
│   ├── data.py              fetch_ohlcv, resample, cache
│   └── utils.py             generate_run_id（backtest + live 共用）
│
├── backtest/                回測 runtime
│   ├── __init__.py
│   ├── engine.py            Backtest + build_output
│   ├── schema.py            Output, RunMetadata, StrategyMetrics dataclasses + validation
│   └── persistence.py       save/load JSON+CSV+Parquet（合併 archive.py）
│
├── live/                    live/sim runtime
│   ├── __init__.py
│   ├── engine.py            LiveTrader（polling loop，即時交易引擎）
│   ├── executor.py          Executor（wrap core.make_fill + Telegram 通知）
│   └── wiring.py            build_live_trader
│
├── config/
│   ├── __init__.py
│   └── market_config.py     MarketConfig, get_market
│
├── notifications/
│   ├── __init__.py
│   └── telegram.py
│
└── cli.py                   base_parser（backtest + live 共用）
```

### 設計理由

- **`core/`**：`strategy.py`, `cost_model.py`, `executor.py`, `metrics.py`, `data.py` 是 backtest 和 live 都依賴的 domain model + 共用計算。`utils.py` 放 `generate_run_id` 等共用小工具。
- **`backtest/`**：回測專屬。schema（batch output 定義）、persistence（JSON/CSV/Parquet 序列化）。不含 metrics（已上移到 core）。
- **`live/`**：live/sim 專屬。LiveTrader polling loop，Executor 加通知 side effect。
- **依賴方向**：`backtest/` 和 `live/` 都依賴 `core/`，彼此不互相依賴。
- **命名一致性**：兩邊都有 `engine.py`（主引擎）。class name 用 domain 語意：`Backtest`（回測器）、`LiveTrader`（即時交易引擎），不用 generic name。Live 有自己的 `executor.py`（加通知），backtest 直接用 `core.executor.make_fill`。

### 共用計算模組

**`core/executor.py`** — 執行邏輯（trade PnL）：
```python
def calc_trade_pnl(
    entry_price: float,
    exit_price: float,
    quantity: float,
    side: str,
    cost_model: CostModel,
    entry_commission: float,
    entry_slippage: float,
) -> TradePnL:
    """Single trade PnL breakdown. Used by backtest + live."""
```

修復目前 live 的 bug：缺 tax 計算、commission 用 `* 2` 簡化不精確。

**`core/metrics.py`** — 指標計算：
```python
def compute_all(
    equity_curve: Sequence[float],
    trades: Sequence[...],
    start_ts: datetime,
    end_ts: datetime,
    annualize: bool = False,
    benchmark_curve: Sequence[float] | None = None,
) -> StrategyMetrics:
    """Compute all metrics from equity curve + trades.

    Called once by backtest (at build_output time).
    Called periodically by live (based on monitoring frequency).
    """
```

### Backtest workflow

```
Backtest.run()
  ├── 每根 bar：記錄 raw equity value（只是數字，不算 metrics）
  └── 平倉時：core.executor.calc_trade_pnl()

Backtest.build_output()
  ├── core.metrics.compute_all()          ← 一次算完所有指標
  └── 組裝 BacktestOutput
```

### Live workflow

```
LiveTrader.run()  (polling loop)
  ├── 每根 bar：記錄 raw equity value → on_bar callback → write DB
  ├── 平倉時：core.executor.calc_trade_pnl() → on_trade callback → write DB
  └── 定期（監控頻率）：core.metrics.compute_all(累積 equity + trades)
      → 更新 dashboard / alert
```

### Current → Target file mapping

```
CURRENT                               TARGET
───────                               ──────
librae/strategy.py              ──→   librae/core/strategy.py
librae/cost_model.py            ──→   librae/core/cost_model.py
librae/executor.py              ──→   librae/core/executor.py (+ calc_trade_pnl)
librae/metrics.py               ──→   librae/core/metrics.py (共用，上移)
librae/data.py                  ──→   librae/core/data.py
librae/utils.py (generate_run_id) ──→ librae/core/utils.py

librae/engine.py                ──→   librae/backtest/engine.py (+ build_output)
librae/schema.py                ──→   librae/backtest/schema.py (+ contracts.py validation)
librae/contracts.py             ─┘
librae/persistence.py           ──→   librae/backtest/persistence.py (+ archive.py)
librae/archive.py               ─┘

librae/live_runner.py           ──→   librae/live/engine.py (LiveTrader)
librae/live_executor.py         ──→   librae/live/executor.py (Executor)
librae/sim_wiring.py            ──→   librae/live/wiring.py (build_live_trader)

librae/scoring.py               ──→   DELETE (only runner uses it)
librae/runners.py               ──→   DELETE (研究階段用 vectorbt)
```

**Files deleted**: `scoring.py`, `runners.py`
**Files merged**: `contracts.py` → `backtest/schema.py`, `archive.py` → `backtest/persistence.py`
**Files moved up**: `metrics.py` → `core/metrics.py`（backtest + live 共用）

### Migration details

**`core/executor.py`** absorbs:
- 新增 `calc_trade_pnl()`：從 backtest `_close_position()` 和 live `_record_trade()` 抽出共用的 PnL 計算邏輯
- 新增 `TradePnL` dataclass：計算結果的回傳型別

**`core/metrics.py`**（從 `librae/metrics.py` 上移）:
- `compute_all()`：backtest 在 `build_output()` 時呼叫一次；live 根據監控頻率定期呼叫
- `_infer_annual_periods()`：保留，年化推算邏輯

**`backtest/schema.py`** absorbs:
- `contracts.py`: `SCHEMA_VERSION`, `REQUIRED_BACKTEST_TOP_LEVEL_KEYS`, `parse_utc_timestamp()`, `check_schema_compat()`
- 不含 persistence 邏輯（SRP：schema 只定義資料結構 + validation）

**`backtest/persistence.py`** absorbs:
- `persistence.py`: `save_output()`, `load_output()`
- `archive.py`: `save_parquet()`
- 獨立函式，不是 BacktestOutput 的 method

**`backtest/engine.py`** absorbs:
- `utils.py`: `build_backtest_output()` logic → becomes `Backtest.build_output()` method
- 平倉時呼叫 `core.executor.calc_trade_pnl()` 取代 inline 計算

**`live/engine.py`**（LiveTrader）:
- 平倉時呼叫 `core.executor.calc_trade_pnl()` 取代 inline 計算（修復 tax 遺漏 + commission 不精確）
- 定期呼叫 `core.metrics.compute_all()` 取代 inline 算 drawdown/ret_1d

**`core/utils.py`**:
- `generate_run_id()`（backtest + live 共用）

### 刪除 runners 的理由

- `runners.py`（`run_strict_protocol`, `run_walkforward`, `run_stability`）零個 production call site，只有測試在用
- 策略研究/參數掃描/穩健性檢測會用 vectorbt 等向量化工具，逐筆回測引擎不適合做大量探索
- `scoring.py`（`score()`, `validate_metrics()`）只有 runner 在用，一併刪除
- 對應的測試也一併刪除：`test_runners.py`, `test_backtest_adapter.py` 中的 runner 相關測試, `test_regression_baselines.py`, `test_research_modules.py`

## Part B: API Redesign

### Step 1: Remove benchmark from BacktestResult

**File: `librae/backtest/engine.py`**

```python
@dataclass(frozen=True)
class BacktestResult:
    trades: Sequence[TradeResult]
    equity_curve: Sequence[EquitySnapshot]
    initial_balance: float
    final_equity: float
    # benchmark_curve removed — stays on Backtest.self
```

### Step 2: `add_benchmark()` — explicit price series input

**File: `librae/backtest/engine.py`**

```python
def add_benchmark(self, prices: pd.Series) -> None:
    """Set benchmark for comparison.

    Args:
        prices: Price series indexed by datetime. Engine computes
                buy-and-hold equity curve in build_output(), aligned
                to backtest timeline.
    """
    self._benchmark_prices = prices
```

- 接收 **price series**，不是 equity curve — 使用者手上有的是 price data
- Buy-and-hold equity curve 在 `build_output()` 時計算，不在 `run()` 裡 — benchmark 跟回測執行無關，是 output/analysis 階段的事
- 沒呼叫 `add_benchmark` → `metrics.benchmark_return` 為 `None`，equity curve 的 benchmark 欄位也為 `None`

### Step 3: Backtest constructor 簡化

**File: `librae/backtest/engine.py`**

```python
def __init__(
    self,
    data: pd.DataFrame,
    strategy: BaseStrategy,
    market_config: MarketConfig,
    initial_balance: float = 100_000.0,
) -> None:
```

- `strategy_name`: **刪除** — 直接用 `type(strategy).__name__` 轉 snake_case，不允許 override
- `symbol`: **自動** — 從 `self._instruments[0]` 取
- `timeframe`: **自動推導** — 從 data index 的 median timedelta 映射到標準 label（M1/M5/H1/D1/W1），推導失敗時 raise error
- `data_source`: **刪除** — 資料來源追蹤不是 engine 的職責
- `market`: 改為 `market_config: MarketConfig` — 直接傳入配置物件
- `executor`: **刪除** — backtest 直接用 `core.executor.make_fill`，不需要自訂 executor

### Step 4: Timeframe 自動推導

**File: `librae/backtest/engine.py`**（module-level private function）

```python
def _infer_timeframe(index: pd.DatetimeIndex) -> str:
    """Infer bar frequency label from data index.

    Uses median timedelta to map to standard labels.
    Logs inferred result. Raises ValueError if not mappable.
    """
    # median delta → label mapping with tolerance
    # e.g. ~60s → "M1", ~300s → "M5", ~3600s → "H1", ~86400s → "D1"
```

Module-level private function，不是 `@staticmethod` — 它是 utility，不需要掛在 class 上。

### Step 5: `run()` 生成 run_id

**File: `librae/backtest/engine.py`**

```python
def run(self) -> BacktestResult:
    """Execute backtest. Generates run_id at start."""
    self._run_id = generate_run_id(self._strategy_name, self._symbol)
    logger.info("Backtest started: run_id=%s", self._run_id)
    ...
```

run_id 在 `run()` 時生成，不在 `__init__()` — 語意上代表「這次執行的 ID」，timestamp 應該是 execution time。

### Step 6: Add `build_output()` to Backtest

**File: `librae/backtest/engine.py`**

```python
def build_output(
    self,
    *,
    annualize: bool = False,
) -> BacktestOutput:
    """Compute metrics + build canonical output in one call.

    - run_id: 在 run() 時已生成
    - start_ts/end_ts: 從 self._timeline 取
    - symbol: 從 self._instruments[0] 取
    - strategy_name: 從 type(strategy).__name__ 取
    - timeframe: 從 data index 自動推導
    - benchmark: 若有 _benchmark_prices，計算 buy-and-hold equity curve
    - Raises RuntimeError if called before run().
    """
```

Internally:
1. 若有 `_benchmark_prices`，計算 buy-and-hold equity curve 並對齊到 backtest timeline
2. Calls `core.metrics.compute_all(equity_curve, trades, start_ts, end_ts, annualize, benchmark_curve=...)`
3. Builds RunMetadata（所有欄位自動推導）
4. Builds TradeRecords, enriched EquityCurvePoints (with ret_1d, drawdown, benchmark alignment)
5. Returns `BacktestOutput`

Also exposes intermediate results as properties for logging:
```python
@property
def result(self) -> BacktestResult: ...  # raises if not run yet

@property
def metrics(self) -> StrategyMetrics: ...  # raises if build_output not called yet
```

### Step 7: Update RunMetadata — 瘦身

**File: `librae/backtest/schema.py`**

```python
@dataclass(frozen=True)
class RunMetadata:
    run_id: str
    strategy: str          # auto: type(strategy).__name__ → snake_case
    symbol: str            # auto: from data index
    timeframe: str         # auto: inferred from data
    start_ts: datetime     # auto: from timeline
    end_ts: datetime       # auto: from timeline
    run_ts: datetime       # auto: now()
    schema_version: str = BACKTEST_SCHEMA_VERSION
    # 已刪除: mode（engine 只做 backtest）, data_source（不是 engine 職責）, sample（不是 engine 職責）
```

### Step 8: Simplify strategy run.py

**Before** (10 lines of pipeline):
```python
result = bt.run()
timeline = sorted(df.index.get_level_values("datetime").unique())
start_ts = timeline[0].to_pydatetime()
end_ts = timeline[-1].to_pydatetime()
metrics = compute_all(result, start_ts, end_ts, annualize=not args.no_annualize)
run_id = generate_run_id(...)
output = build_backtest_output(result, metrics, run_id=run_id, ...)
paths = save_backtest_output(output, Path(args.out_dir))
if not args.no_db:
    write_backtest_output(output)
    write_ohlcv(...)
```

**After**:
```python
from librae.backtest import Backtest
from librae.backtest.persistence import save_output
from db.timescale_writer import write_backtest_output, write_ohlcv

bt = Backtest(
    data=df,
    strategy=strategy,
    market_config=market_config,
    initial_balance=args.initial_balance,
)
bt.add_benchmark(benchmark_prices)   # optional, explicit price series
bt.run()

output = bt.build_output()
save_output(output, Path(args.out_dir))
if not args.no_db:
    write_backtest_output(output)
    write_ohlcv(...)  # stays separate — ohlcv is not part of backtest output
```

### Step 9: Update `__init__.py` exports

**頂層 `librae/__init__.py` re-export（便利 shortcut）：**
```python
from librae.backtest.engine import Backtest
from librae.backtest.schema import BacktestOutput, RunMetadata, StrategyMetrics
from librae.backtest.schema import TradeRecord, EquityCurvePoint
from librae.core.strategy import BaseStrategy, Action, Context, Fill, Position
from librae.core.cost_model import CostModel
from librae.config.market_config import MarketConfig, get_market, load_market_configs
```

**Removed from exports:**
- `build_backtest_output` (now `Backtest.build_output()`)
- `save_backtest_output`, `load_backtest_output` (now `backtest.persistence.save_output/load_output`)
- `generate_run_id` (auto-generated in `run()`, 進階用途從 `core.utils` import)
- `metrics_dict_to_backtest_output` (deleted with runners)
- `run_strict_protocol`, `run_walkforward`, `run_stability`, `make_backtest_fn` (deleted)
- `score`, `validate_metrics`, `REQUIRED_METRICS_KEYS` (deleted with scoring.py)
- `Periods`, `WFWindow` (deleted with runners)

### Step 10: Update tests

- 透過 `build_output()` 的 output 驗證 benchmark 行為，不測試 private attribute `_benchmark_curve`
- Add test: `bt.build_output()` produces valid `BacktestOutput`
- Add test: `save_output()` roundtrip with `load_output()`
- Add test: calling `build_output()` before `run()` raises RuntimeError
- Add test: `_infer_timeframe` correctly maps known bar frequencies
- Add test: `add_benchmark` → output 有 benchmark_return; 沒呼叫 → benchmark_return is None
- Add test: `core.executor.calc_trade_pnl` 結果與現有 backtest `_close_position` 一致
- Delete: `test_runners.py`, `test_backtest_adapter.py` (runner tests), `test_regression_baselines.py`, `test_research_modules.py`

## Output Structure Reference

```
BacktestOutput
├── run_metadata: RunMetadata
│   ├── run_id, strategy, symbol, timeframe
│   ├── start_ts, end_ts, run_ts
│   ├── schema_version
│
├── equity_curve: list[EquityCurvePoint]
│   └── [ts, equity, ret_1d, drawdown, benchmark_equity, benchmark_ret_1d]
│
├── trades: list[TradeRecord]
│   └── [trade_id, entry_ts, exit_ts, symbol, side, entry_price, exit_price,
│        quantity, gross_pnl, net_pnl, gross_return, net_return,
│        commission, slippage, holding_bars, price_unit, quantity_unit, pnl_unit]
│
├── metrics: StrategyMetrics
│   └── [total_return, annual_return, sharpe, sortino, calmar, max_drawdown,
│        trades, win_rate, profit_factor, avg_trade_return, exposure_ratio,
│        benchmark_return, total_commission, total_slippage]
```

Persistence（獨立函式，不在 BacktestOutput 上）：
- `save_output(output, dir)` → JSON + CSV + Parquet
- `load_output(path)` → BacktestOutput
- `write_backtest_output(output)` → TimescaleDB（留在 `db/timescale_writer.py`）

## Parameter Placement

| 參數 | 放在哪 | 原因 |
|------|--------|------|
| `data` | `__init__()` | 必要依賴 |
| `strategy` | `__init__()` | 必要依賴 |
| `market_config` | `__init__()` | 必要依賴（成本模型 + market metadata） |
| `initial_balance` | `__init__()` | 預設 100,000 |
| `symbol` | **自動** | 從 `data.index` instrument level 取 |
| `start_ts` / `end_ts` | **自動** | 從 `self._timeline` 取 |
| `strategy_name` | **自動** | 從 `type(strategy).__name__` 轉 snake_case |
| `timeframe` | **自動** | 從 data index median timedelta 推導 |
| `run_id` | **自動** | `run()` 時生成（strategy + symbol + timestamp） |
| `benchmark` | `add_benchmark(prices)` | optional method，接收 price series，build_output 時算 buy-and-hold |
| `annualize` | `build_output()` 預設 `False` | opt-in，資料太短時年化 misleading |

**已刪除的參數**：
- `strategy_name`: 不需要 override，直接用 class name
- `data_source`: 資料來源追蹤不是 engine 職責
- `sample` / `split`: 策略驗證流程不是 engine 職責
- `mode`: engine 只做 backtest，不需要區分
- `executor`: backtest 直接用 core.executor.make_fill

## Design Decisions Log

### D1: 刪除 runners.py + scoring.py
- **決定**：完全移除，不是 deprecate
- **原因**：零個 production call site；策略研究/穩健性檢測會用 vectorbt 等向量化工具；逐筆回測引擎不適合做大量參數探索
- **影響**：刪除對應測試（test_runners, test_backtest_adapter runner 部分, test_regression_baselines, test_research_modules）

### D2: timeframe 自動推導而非必填
- **決定**：從 data index median timedelta 推導，module-level private function `_infer_timeframe()`，推導失敗 raise error
- **原因**：`_infer_annual_periods` 已證明可從 bar 間隔推導年化週期，同理可映射到 label
- **風險**：缺漏資料可能導致推導錯誤 → 推導後 log warning 供使用者確認

### D3: annualize 預設 False
- **決定**：`compute_all` 和 `build_output` 的 annualize 預設改為 False
- **原因**：短資料年化後數字 misleading，使用者應明確 opt-in
- **備註**：現有 `_infer_annual_periods` 用實際 bar 密度推算年化週期，對 crypto 24h 和有休市的市場都合理。暫不在 MarketConfig 加 `trading_days_per_year`（YAGNI），未來有系統性偏差再加

### D4: 刪除 data_source、sample、mode 欄位
- **決定**：從 RunMetadata 移除
- **原因**：`data_source` 是 data pipeline 的職責；`sample`/`split` 是策略研究流程的標籤；`mode` 永遠是 "backtest"（live/sim 是不同的 runtime path，不共用 BacktestOutput）

### D5: strategy_name 不可 override
- **決定**：直接用 `type(strategy).__name__`，不提供 override 參數
- **原因**：同一 class 不同參數的區分靠 `run_id`，不需要另外取名

### D6: add_benchmark(prices) 取代 set_benchmark("auto")
- **決定**：benchmark 改為 explicit price series input，engine 在 build_output() 時計算 buy-and-hold
- **原因**：使用者完全控制 benchmark 是什麼。接收 price series（不是 equity curve）因為使用者手上有的是 price data。buy-and-hold 計算含時間軸對齊，engine 做比使用者做更不容易出錯

### D7: Persistence 不放在 BacktestOutput 上
- **決定**：`save_output()`, `load_output()` 為獨立函式在 `backtest/persistence.py`，DB 寫入留在 `db/timescale_writer.py`
- **原因**：SRP — domain model 不依賴 infrastructure。`write_db()` 放在 dataclass 上會讓 schema.py import db layer，依賴方向錯誤。改基礎設施不該動 schema.py

### D8: run_id 在 run() 時生成
- **決定**：`run_id` 在 `run()` 開始時生成，不在 `__init__()` 或 `build_output()`
- **原因**：語意上代表「這次執行的 ID」，timestamp 應該是 execution time。run 之前不需要 run_id，build_output 太晚（run 中途需要 log）

### D9: generate_run_id 放 core/utils.py
- **決定**：backtest 和 live 都需要 generate_run_id，放在 `core/utils.py`
- **原因**：不屬於任何特定 domain 概念，但多處共用。core/ 內的 utils.py 有明確 scope（core 層級小工具），不是頂層萬用垃圾桶

### D10: Package 結構 — core / backtest / live 三層
- **決定**：共用 domain model 抽到 `core/`，backtest 和 live 各自為 subpackage
- **原因**：backtest（batch，一次跑完）和 live（polling loop，持續運行）是不同的 runtime path。檔名對齊（都有 engine.py），class name 用 domain 語意（Backtest / LiveTrader）

### D11: persistence.py 合併 archive.py
- **決定**：`archive.py`（Parquet）併入 `backtest/persistence.py`（JSON+CSV+Parquet）
- **原因**：都是「把 BacktestOutput 序列化到磁碟」，修改理由相同（schema 變了），~180 行合併後仍然精簡

### D12: metrics.py 上移到 core/，calc_trade_pnl 放 core/executor.py
- **決定**：`compute_all` 從 `backtest/metrics.py` 上移到 `core/metrics.py`；trade PnL 計算抽到 `core/executor.py` 的 `calc_trade_pnl()`
- **原因**：
  - `compute_all` 是共用的：backtest 在 `build_output()` 時一次算完，live 根據監控頻率定期算。同一個函式，不同觸發時機
  - Trade PnL 是執行邏輯（平倉計算），不是 metrics。目前 backtest `_close_position()` 和 live `_record_trade()` 各寫一份，live 有 bug（缺 tax、commission `* 2` 不精確）
  - `backtest/metrics.py` 刪除，不再需要

### D13: LiveTrader 命名
- **決定**：live 主引擎 class name 從 `Runner` 改為 `LiveTrader`
- **原因**：`Runner` 太 generic。`LiveTrader` 明確表達「即時交易引擎」，sim 和 live 的差異只是 executor 的 `simulation` flag，主引擎相同。與 `Backtest` 同樣用 domain 語意命名

## Files to Modify (ordered)

1. 建立 `librae/core/` — 搬入 strategy.py, cost_model.py, executor.py（+ calc_trade_pnl）, metrics.py（上移 + 共用化）, data.py；新增 utils.py（generate_run_id）
2. 建立 `librae/backtest/` — 搬入 engine.py, schema.py（+ contracts.py）, persistence.py（+ archive.py）
3. 建立 `librae/live/` — 搬入 live_runner.py → engine.py (LiveTrader), live_executor.py → executor.py, sim_wiring.py → wiring.py
4. `librae/core/executor.py` — 新增 calc_trade_pnl + TradePnL dataclass
5. `librae/core/metrics.py` — 調整 compute_all 為 backtest + live 共用介面
6. `librae/backtest/schema.py` — absorb contracts.py validation；瘦身 RunMetadata（刪 mode, data_source, sample）
7. `librae/backtest/engine.py` — remove benchmark from BacktestResult; 簡化 constructor; add _infer_timeframe + add_benchmark + build_output(); run_id 在 run() 生成; 平倉用 core.executor.calc_trade_pnl
8. `librae/backtest/persistence.py` — 合併 archive.py; 改為獨立函式 save_output/load_output
9. `librae/live/engine.py` — rename LiveRunner → LiveTrader; 平倉用 core.executor.calc_trade_pnl; 定期用 core.metrics.compute_all
10. `librae/live/wiring.py` — rename build_sim_runner → build_live_trader
11. `librae/__init__.py` — re-export from subpackages
12. `strategies/trendpullback/run.py` — simplify to new API
13. `strategies/trendpullback_m5/run.py` — simplify to new API
14. `tests/` — update existing tests + delete runner/scoring tests + add calc_trade_pnl test
15. Delete from root: all original files that were moved to subpackages

## Part C: README.md 策略開發範例更新

**File: `README.md`**

更新「策略開發範例」section，展示優化後的使用方式：

```python
from librae import Backtest, BaseStrategy
from librae.backtest.persistence import save_output
from librae.config import get_market

# 1. 建立引擎
market_config = get_market("crypto")
bt = Backtest(
    data=df,
    strategy=MyStrategy(),
    market_config=market_config,
)
bt.add_benchmark(btc_prices)   # optional: price series, engine computes buy & hold

# 2. 執行回測
bt.run()

# 3. 產出完整結果（metrics + 規範化輸出，一步完成）
output = bt.build_output()

# 4. 查看結果
print(output.metrics.sharpe, output.metrics.max_drawdown)

# 5. 持久化（opt-in）
save_output(output, "results/")   # → JSON + CSV + Parquet
```

## Git Workflow

1. 在 `refactor/engine-framework` 分支上開發
2. 按 Part A → B → C 順序提交，每個 Part 一個 commit
3. 全部完成後開 PR 回 `main`

## Deliverables

1. **程式碼**：librae 框架重構（package 結構重組 + 共用計算模組 + 檔案合併 + 刪除 runner/scoring + API 改造 + 策略 run.py 簡化）
2. **README.md**：策略開發範例更新為優化後版本

## Verification

1. `python -m pytest tests/` — all existing tests pass (runner/scoring tests already deleted)
2. `python -m strategies.trendpullback.run --mode backtest --dry-run` — new API works
3. `python -m strategies.trendpullback_m5.run --mode backtest --dry-run` — new API works
4. Verify `save_output()` produces identical JSON/CSV as before
5. Verify `load_output()` roundtrip preserves all values
6. Verify `_infer_timeframe` correctly maps M1/M5/H1/D1/W1
7. Verify benchmark: `add_benchmark` → output 有 benchmark_return; 沒呼叫 → None
8. Verify `core.executor.calc_trade_pnl` 結果與現有 backtest `_close_position` 一致
9. Verify live `LiveTrader` 平倉計算含 tax（修復現有 bug）
