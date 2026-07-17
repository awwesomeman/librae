# Refactor Librae API (Unified Execution Framework)

> 狀態：implemented
> 範圍：engine, cli, live, backtest, db
> 建立日期：2026-04-05
> 最後更新：2026-04-06
> 依據：[refactor_librae](refactor_librae.md)

## 重構目標

使用者調用回測引擎 API 一致性，以及 API 底層邏輯、模組調用一致性。

## 重構原則：直接替換，不保留舊介面

本計劃為大幅重構，執行時遵循以下原則：

- **不做 DB migration** — `timescale_init.sql` 直接改 schema，部署時 drop + recreate
- **不保留舊函式簽名** — `build_live_trader`、舊版 `compute_all(annualize=)` 等直接刪除，不做 deprecated wrapper
- **不加 backward-compat shim** — 舊的 `run_backtest(args)` / `run_sim(args)` 簽名直接改為 `run_backtest(cfg)` / `run_realtime(cfg)`，不保留 `args` 版本
- **不保留 fallback 路徑** — `annual_periods` 必傳不留 `None` 推導、`wiring.py` 直接刪除不留 re-export
- **移除即刪除** — 被取代的模組（`wiring.py`）、被合併的函式（`run_sim` + `run_live` → `run_realtime`）直接刪，不留 `# removed` 註解或 `_deprecated` alias
- **例外：`Backtest` 保留 legacy 參數** — tests 大量使用 `Backtest(data, strategy, cost_model=...)` 不需 RunConfig 的用法。`cfg=None` 時走 legacy path，避免所有 test 都要構造 RunConfig

---

## 實作狀態

> 最後更新：2026-04-06

### ✅ 已完成

| 項目 | 狀態 |
|------|------|
| §1 RunConfig dataclass | ✅ 已實作。`@cached_property` 用於 `config_hash` 和 `perf_params` |
| §2 SignalPoller | ✅ 已實作。HoldStrategy 已刪除 |
| §3 引擎接收 cfg | ✅ Backtest + LiveTrader 都已改。LiveTrader 用 `_UNSET` sentinel + data-driven callback loop |
| §4 compute_all 新參數 | ✅ `risk_free_rate`, `annual_periods`, `ddof` 已加 |
| §4b config_hash dedup | ✅ `with_dedup_check` + `check_existing_run`（委託 `refresh_performance` 重算） |
| §5 統一 runner 簽名 | ✅ 4 個 run.py 全部改用 `run_backtest(cfg)` / `run_realtime(cfg)` |
| §5b run_dispatch | ✅ 共用 `main()` |
| §6 CLI build_config | ✅ `floor_to_timeframe`, periods→start/end, `--force`, `--no-annualize` |
| 命名 _bars→_periods | ✅ 全面改名（含 DB column、tests、grafana、scripts） |
| DB schema | ✅ `config_hash VARCHAR(32) UNIQUE` + `perf_params JSONB` + `holding_periods` |
| wiring.py 移除 | ✅ 已刪 |
| config.yaml perf 區 | ✅ 所有 config.yaml 都有 perf section |

### 🔲 尚未實作（不阻擋本次重構）

| 項目 | 原因 | 後續行動 |
|------|------|----------|
| `librae/live/fetch_cache.py` | LiveTrader 和 SignalPoller 各有 `_fetch_with_cache`（~30 行，98% 相同）。目前符合 Rule of Three — 只有 2 個使用者。且 SignalPoller 未來可能加 DB-first warmup（與 LiveTrader 合流），到時再抽更自然 | 等第三個使用者出現或 SignalPoller 加 warmup 時再抽 |
| `librae/core/stats.py` | 目前 `compute_all` 完全委託 QuantStats，沒有手寫的統計函式需要共用。只有在 `signal_metrics.py` 需要部分相同的統計計算時，抽出才有意義 | 隨 signal_metrics 一起實作 |
| `librae/core/signal_metrics.py` | `compute_signal_metrics`（hit rate、forward return 等）需搭配 `fix_look_ahead_bias.md` 的 next-bar execution 設計，現在實作容易做錯前視偏誤 | 搭配 fix_look_ahead_bias 另開 issue |
| `ON CONFLICT (config_hash)` 防護 | 目前 `write_backtest_output` 的 upsert 用 `ON CONFLICT (run_id)`，config_hash 只靠 UNIQUE INDEX 擋。單 worker 不會觸發，但多 worker race condition 時會拋 postgres error 而非 silent no-op | 已加 TODO，未來多 worker 排程時處理 |
| `ddof` 接 QuantStats | `compute_all` 接受 `ddof` 但 QuantStats 的 sharpe/sortino 不支援自訂 ddof。目前是 pass-through 佔位 | 已加 TODO，等自己算 Sharpe 時接上 |

---

## Context

四條執行路徑（策略回測/sim、訊號回測/sim）的 API 不一致：參數傳遞方式、函式簽名、指標計算路徑、DB 寫入模式各不相同。需要統一成一致的框架。

### 現狀問題（重構前）

| | 策略回測 | 策略 sim | 訊號回測 | 訊號 sim |
|---|---|---|---|---|
| 入口函式 | `run_backtest(args)` | `run_sim(args)` | `run_backtest()` 無參數 | `run_sim()` 無參數 |
| 引擎 | `Backtest.run()` | `LiveTrader.run()` | 無 | `LiveTrader + HoldStrategy` hack |
| 指標計算 | `build_output → compute_all` | `refresh_performance → compute_all` | 無 | 無（HoldStrategy 不觸發） |
| annualize | CLI `--no-annualize` 控制 | 寫死 `True` | N/A | N/A |
| DB 寫入 | `save_strategy_results` 原子寫入 | 逐筆 callbacks | `save_signal_results` | 逐筆 callbacks + 無意義 equity |

---

## 設計原則

1. **參數分三類**：
   - **策略參數**（存 DB `backtest_runs.params`）— 影響回測結果：`max_hold_periods`、`warmup_periods`、`fill_price`
     > 注意：`periods` 不是策略參數 — 它是指定資料視窗的方式，由 `build_config` 轉換為 `start`/`end` 後 pop 掉，不存 DB
   - **績效參數**（存 DB `backtest_runs.perf_params`）— 只影響報表呈現，不影響回測結果：`risk_free_rate`、`annual_periods`、`ddof`、`annualize`
   - **行為參數**（不存 DB，啟動時 log 顯示）— `no_db`、`dry_run`（= no_db + 不送 Telegram）
2. **啟動時明確顯示所有設定** — 使用者一眼知道自己用了什麼參數，不管是回測還是 sim
3. **不過度抽象** — 引擎主迴圈邏輯不動，wiring 內聚到 `__init__`

---

## 核心改動

### 1. `RunConfig` dataclass（`librae/core/run_config.py`）

統一的參數容器，從 CLI + config.yaml 合併產生，貫穿所有路徑。

```python
@dataclass(frozen=True)
class RunConfig:
    # === 策略識別（存 DB） ===
    strategy_name: str
    symbols: list[str]
    timeframe: str
    market: str
    data_source: str
    initial_balance: float
    mode: Literal["backtest", "sim", "live"]
    start: str | None = None
    end: str | None = None
    params: dict[str, Any] | None = None
    cost_overrides: dict[str, float] | None = None

    # === 績效參數（存 DB backtest_runs.perf_params，只影響報表呈現） ===
    annualize: bool = True
    risk_free_rate: float = 0.0
    annual_periods: int = 365         # 每年交易日數（crypto=365, 台股=252），非 bar 數
    ddof: int = 1                     # TODO: 尚未接 QuantStats，目前是 pass-through 佔位

    # === 行為參數（不存 DB，啟動時 log 顯示） ===
    poll_seconds: int = 60
    no_db: bool = False
    dry_run: bool = False
    force: bool = False
    telegram_config: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.dry_run and not self.no_db:
            raise ValueError("dry_run=True requires no_db=True; use build_config()")

    @property
    def symbol(self) -> str: ...

    @cached_property
    def perf_params(self) -> dict[str, Any]: ...

    @cached_property
    def config_hash(self) -> str:
        """Deterministic hash via _sanitize_for_hash + float.hex()."""
        ...

    def log_summary(self) -> None:
        """三區顯示 + config_hash + code_rev (git describe --always --dirty)."""
        ...
```

> **實作決策**：
> - `config_hash` 和 `perf_params` 用 `@cached_property`（比 plan 原本的 `@property` 更好，避免重複計算）
> - `log_summary` 中 `_get_code_rev()` 用單一 `git describe --always --dirty`（比原先兩次 subprocess 更高效）
> - `signal_column` 不放 RunConfig — 由策略自行定義
> - `fill_price` 歸入 `params`（策略參數）
> - RunConfig 是純參數容器，**不包含 `fetch_data` 等 I/O 方法**

### 2. `SignalPoller`（`librae/live/signal_poller.py`）

輕量的訊號 sim poller，取代 `LiveTrader + HoldStrategy` hack：
- **只做**：poll → feature_fn → 抽 signal → write signal_events + ohlcv + heartbeat
- **不做**：策略決策、部位管理、equity curve、trade_events、Telegram

約 200 行（含 `_db_write` helper 和 `_fetch_with_cache`）。

> **與 plan 原案的差異**：`_fetch_with_cache` 未抽成獨立 `fetch_cache.py`。LiveTrader 和 SignalPoller 各自維護 ~30 行的 `_fetch_with_cache`（98% 相同，LiveTrader 多 `warmup_fetcher` fallback）。目前符合 Rule of Three。SignalPoller 內部用 `_db_write()` 統一 DB 錯誤處理，與 LiveTrader 同 pattern。

### 3. 引擎直接接收 `cfg`，移除 `build_live_trader`

wiring 邏輯內聚到引擎 `__init__`，使用者直接建構：

```python
# 最簡用法 — 預設 CryptoAdapter + 從 market 推導 cost
sim = LiveTrader(strategy, prepare_signals, cfg=cfg)
sim.run()

# 訊號 sim — SignalPoller 同樣模式
poller = SignalPoller(prepare_signals, cfg=cfg)
poller.run()
```

`LiveTrader.__init__` 用 `_UNSET` sentinel pattern 區分「未傳」vs「明確傳 None」：

```python
_UNSET = object()

class LiveTrader:
    def __init__(
        self,
        strategy: BaseStrategy,
        feature_fn: Callable,
        *,
        cfg: RunConfig,
        adapter: OHLCVFetcher | None = None,
        cost_model: CostModel | None = None,
        on_bar: Callable | None = _UNSET,
        ...
    ):
```

Callback 解析用 data-driven loop 避免 6 組重複 if/else：
```python
callbacks = {
    "on_bar": (on_bar, self._build_on_bar),
    "on_order_event": (on_order_event, self._build_on_order_event),
    ...
}
for attr, (value, builder) in callbacks.items():
    if value is not _UNSET:
        setattr(self, f"_{attr}", value)
    elif cfg.no_db:
        setattr(self, f"_{attr}", None)
    else:
        setattr(self, f"_{attr}", builder())
```

> **Backtest 保留 legacy 參數**：`Backtest.__init__` 接受 `cfg=None`，此時走 `market_config=` / `initial_balance=` / `data_source=` 的 legacy path。這是刻意的偏差 — tests 大量使用 `Backtest(data, strategy, cost_model=CostModel.zero(), data_source="test")`，強制構造 RunConfig 會讓測試寫法笨重。`Backtest` 作為 library 級 API，讓外部使用者不需要 RunConfig 也能用是合理的。

### 4. `refresh_performance` + `compute_all` 參數傳遞

`db/timescale_writer.py`：

```python
def refresh_performance(run_id: str, cfg: RunConfig | None = None, dsn=...) -> None:
    # 當 cfg 提供時，用 cfg.perf_params 傳給 compute_all
```

`librae/core/metrics.py` 的 `compute_all` 新增參數：

```python
def compute_all(
    ...,
    annualize: bool = False,
    risk_free_rate: float = 0.0,
    annual_periods: int = 365,
    ddof: int = 1,   # TODO: 尚未接 QuantStats
) -> StrategyMetrics:
```

> **annual_periods 實作決策**：plan 原本要求 `compute_all` 內部做 `annual_periods × periods_per_day(timeframe)` 乘法。實作改為完全委託 `_infer_annual_periods(ts_index)` 從實際資料密度推算 bars/year。
>
> **這比 plan 更好**的原因：inferred 值直接反映實際資料密度。台股（5h/day, 252 days）inferred = 1260 bars/year（正確），而 plan 的做法會算出 252×24 = 6048（錯誤，台股不是 24h 交易）。crypto 24/7 兩者相同。
>
> `annual_periods` 目前用途：存入 DB `perf_params` 作為記錄，供 `check_existing_run` 輕量 recompute 路徑判斷是否需要重算。

### 4b. `config_hash` 重複檢測 + `perf_params` 輕量更新

```python
# librae/cli.py
def with_dedup_check(fn):
    def wrapper(cfg):
        if not cfg.no_db and not cfg.force:
            existing = check_existing_run(cfg)
            if existing:
                return
        fn(cfg)
    return wrapper
```

四條路徑：

| config_hash 在 DB 中 | perf_params | force | 行為 |
|---|---|---|---|
| 不存在 | — | — | 全新 run，正常執行 |
| 存在 | 相同 | False | 完全跳過，log "skipping" |
| 存在 | 不同 | False | 輕量路徑：跳過回測，只重算 metrics |
| 存在 | 任意 | True | 強制全新計算 |

> **實作決策**：`check_existing_run` 的輕量 recompute 路徑直接呼叫 `refresh_performance(run_id, cfg=cfg)`，而非 plan 原本展開的完整 equity/trades 讀取邏輯。避免重複程式碼。

### 5. 統一 runner 簽名 + `run_dispatch` 共用 main()

所有 `run.py` 統一為：

```python
def run_backtest(cfg: RunConfig): ...
def run_realtime(cfg: RunConfig): ...

def main() -> None:
    from librae.cli import run_dispatch
    run_dispatch(STRATEGY_NAME, __file__, run_backtest, run_realtime)
```

### 6. CLI 更新 + `build_config()` 核心邏輯

`librae/cli.py`：
- `--no-annualize` → 覆蓋 config.yaml 的 `perf.annualize`
- `--dry-run` → 設定 `no_db=True` + 不送 Telegram
- `--force` → 跳過 `find_run_by_config_hash` 檢查
- `fill_price` 不需要 CLI flag — 在 `config.yaml` 的 `params` 裡定義
- 移除 `--signal-column` — 由策略自行定義

`build_config()` 處理：
1. `params.pop("start", "end")` → `RunConfig.start/end`
2. periods → start 轉換（`floor_to_timeframe` 確保 config_hash 穩定）
3. `cost_overrides` 從 strategy section 抽出
4. `dry_run → no_db` 推導
5. `annual_periods` 從 `data_source` 推導預設值（crypto=365, tw=252）

`config.yaml` 策略參數與績效參數分開定義：

```yaml
strategy:
  params:
    periods: 4320
    max_hold_periods: 24
    warmup_periods: 720

  perf:
    risk_free_rate: 0.0
    annual_periods: 365
    ddof: 1
    annualize: true
```

---

## 參數流向

```
config.yaml + CLI flags
       │
       v
 build_config(name, __file__)  ← 合併，產生 frozen RunConfig
       │  1. params.pop("start","end") → RunConfig.start/end
       │  2. periods → start 轉換（若無 start）
       │  3. cost_overrides 從 strategy section 抽出
       │  4. dry_run → no_db 推導
       │
       ├─ cfg.config_hash          ← 確定性 hash（重複檢測用）
       │
       ├─ cfg.log_summary()        ← 啟動時印出：策略 / 績效 / 行為 + config_hash
       │
       ├─ with_dedup_check(run_backtest)  ← dispatch 層攔截重複
       │     → check_existing_run(cfg)：跳過 / 委託 refresh_performance 重算 / 放行
       │
       ├─ [backtest] Backtest(data, strategy, cfg=cfg)
       │     get_ohlcv(start=cfg.start, end=cfg.end, warmup_periods=...)
       │     bt.build_output()  ← 內部從 self._cfg.perf_params 取
       │     save_strategy_results(output, data, cfg)
       │       → DB 存 params + perf_params + config_hash
       │
       ├─ [realtime] LiveTrader(strategy, feature_fn, cfg=cfg)
       │     sim/live 由 cfg.mode + adapter pattern 區分
       │     → cost_overrides merge → CostModel
       │     → 內部 wiring：adapter, callbacks, executor（data-driven loop）
       │     → refresh_performance(run_id, cfg=cfg)
       │
       └─ [signal realtime] SignalPoller(feature_fn, cfg=cfg)
             → 內部 wiring：adapter, _db_write helper
             → write_signal_event + write_ohlcv only
```

---

## 不動的東西

- `Backtest` 引擎主迴圈（`librae/backtest/engine.py`）— 只改 `__init__` 接收 `cfg`
- `process_actions()`（`librae/core/executor.py`）
- DB schema 直接在 `timescale_init.sql` 中加入 `config_hash VARCHAR(32) UNIQUE` + `perf_params JSONB`，部署時 drop + recreate（不做 migration）

---

## 命名統一：`_bars` → `_periods`

配合 `warmup_bars → warmup_periods`（已完成），本次重構一併統一所有 bar 相關命名：

| 舊命名 | 新命名 | 影響範圍 |
|---|---|---|
| `max_hold_bars` | `max_hold_periods` | strategy classes, config.yaml, run.py, utils.py |
| `total_bars` | `total_periods` | metrics.py, backtest/engine.py, timescale_writer.py |
| `exposed_bars` | `exposed_periods` | backtest/engine.py, backtest/schema.py, metrics.py |
| `holding_bars` | `holding_periods` | executor.py, backtest/engine.py, backtest/schema.py, timescale_writer.py, timescale_reader.py |
| `periods_held` | `periods_held` | strategy.py (PositionState, Context), executor.py, live/engine.py, backtest/engine.py |
| `interval_bars` | `interval_periods` | config/notification.py |

> DB column `holding_bars` 在 `trade_events` 表中 → 改為 `holding_periods`（drop + recreate，不做 migration）。

---

## 影響檔案

| 檔案 | 改動 | 狀態 |
|---|---|---|
| `librae/core/run_config.py` | **新增** — RunConfig dataclass | ✅ |
| `librae/live/signal_poller.py` | **新增** — 輕量 signal poller | ✅ |
| `librae/live/fetch_cache.py` | **延後** — `_fetch_with_cache` 共用 helper | 🔲 等第三個使用者 |
| `librae/live/engine.py` | `LiveTrader.__init__` 接收 `cfg`，內聚 wiring | ✅ |
| `librae/live/wiring.py` | **移除** | ✅ |
| `librae/cli.py` | 新增 `build_config()`, `run_dispatch()`, `with_dedup_check()` | ✅ |
| `librae/core/stats.py` | **延後** — 共用統計工具 | 🔲 隨 signal_metrics |
| `librae/core/signal_metrics.py` | **延後** — 須搭配 fix_look_ahead_bias | 🔲 另開 issue |
| `librae/core/metrics.py` | `compute_all` 加 `risk_free_rate`、`annual_periods`、`ddof` | ✅ |
| `db/timescale_writer.py` | `refresh_performance(cfg=)`；`save_strategy_results(output, df, cfg)` | ✅ |
| `db/timescale_reader.py` | 新增 `find_run_by_config_hash()` | ✅ |
| `deploy/timescale_init.sql` | `config_hash` + `perf_params` + `holding_periods` | ✅ |
| `strategies/trendpullback/run.py` | 改用 `RunConfig` | ✅ |
| `strategies/trendpullback_m5/run.py` | 同上 | ✅ |
| `experiments/strategies/trendmaster/run.py` | 同上 | ✅ |
| `experiments/signals/kdj_oversold/run.py` | 改用 `RunConfig` + `SignalPoller`，移除 HoldStrategy | ✅ |

---

## 驗證

- [x] `pytest tests/ -q` 全部通過（210 passed）
- [ ] 跑策略回測，確認 `log_summary` 印出三區（策略/績效/行為）+ start/end + config_hash
- [ ] 跑訊號回測，確認參數顯示正確
- [ ] 確認 DB `backtest_runs.params` 只存策略參數（含 fill_price、warmup_periods）
- [ ] 確認 DB `backtest_runs.perf_params` 只存績效參數（annualize、risk_free_rate、annual_periods、ddof）
- [ ] 確認 DB `backtest_runs.config_hash` 正確寫入
- [ ] 確認行為參數（no_db、dry_run）不進 DB
- [ ] 確認 sim 路徑的 annualize 可由 CLI `--no-annualize` 控制
- [ ] 同參數跑兩次 → 第二次被 `config_hash` 攔截，log "skipping"
- [ ] 同參數但改 `risk_free_rate` → 觸發輕量 recompute，DB `summary_stats` 更新
- [ ] 改 `slippage_ticks`（透過 `cost_overrides`）→ `config_hash` 不同，正常全新 run
- [ ] 確認 `periods` 和 `start`/`end` 兩種模式都正確解析
- [ ] 確認 periods 模式同一 bar 窗口內重複執行 → `config_hash` 相同（`floor_to_timeframe` 生效）
- [ ] 確認 `--force` 跳過快取，覆寫既有結果
- [x] 確認 `check_existing_run` 在引擎啟動前呼叫（策略 + 訊號回測共用）
- [x] 確認 sim/live 路徑不做 config_hash 檢查
- [x] 確認 run_id 只由引擎生成（Backtest.run / LiveTrader.__init__ / SignalPoller.__init__）
- [x] 確認 `RunConfig(dry_run=True, no_db=False)` → raise `ValueError`
- [x] 確認 `_sanitize_for_hash` 使用 `float.hex()`

---

## 防呆注意 (Watch-outs)

### periods 模式的時間精度

`datetime.now()` 帶微秒精度，每次執行產生的 `start` 都不同 → `config_hash` 每次不同 → 快取失效。
`build_config` 中已使用 `floor_to_timeframe(now, tf)` 將 end 退回到 timeframe 邊界（如 14:32:01 H1 → 14:00:00），
start 從 floored end 反推。同一 bar 窗口內重複執行，hash 保持相同。

**sim 銜接注意**：floor 後的 end 切掉了當前未完結的 bar（14:00~14:32 的部分）。
回測模式這是正確的（只用已完結的 bar）。但 LiveTrader 啟動時需要 warmup 歷史資料，
其 `_fetch_with_cache` 已獨立處理 warmup 範圍，不依賴 RunConfig.end，所以不受影響。

### config_hash 浮點精度

`json.dumps` 將 float 轉 string 時，`0.1` vs `0.10000000001` 會產生不同 hash。
已在 `config_hash` property 中加入 `_sanitize_for_hash()`，使用 `float.hex()` 正規化。
`float.hex()` 零精度損失且絕對 deterministic，不受低價幣精度或科學記號常數（如 L2 weight 1e-11）影響。

### config_hash 的邊界：不含 code/data 版本

`config_hash = f(Config)` 而非 `f(Code, Config, Data)`。以下變動**不會**自動觸發重算：
- 策略程式碼修改（修 bug、調指標公式）
- 底層 OHLCV 資料修復（bad ticks）
- `prepare_signals` 邏輯變更

**設計理由**：策略程式碼迭代頻率極高（改一行就是新 hash），若加入 code hash 會讓快取幾乎永遠失效。

**使用者須知**：程式碼或底層資料有變動時，使用 `--force` 強制重算。

### 並發 Race Condition

多個 Worker 同時啟動同一回測時，`find_run_by_config_hash` 與 INSERT 之間可能 race。

**現行對策**：`config_hash` 設為 UNIQUE INDEX，postgres 會在 INSERT 時拋錯，被 try/except 捕獲。
這在單 Worker 流程中不會觸發（`with_dedup_check` 已在 dispatch 層攔截），但多 Worker 時不夠優雅。

**TODO**：改為 `ON CONFLICT (config_hash) DO NOTHING`（正常 flow）和 `ON CONFLICT (config_hash) DO UPDATE`（`--force`），作為 defensive safety net。已在 `write_backtest_output` 加 TODO 註解。

**未來排程系統優化**：長時間回測前先 INSERT 一筆 `status='computing'` 的 placeholder run，利用 UNIQUE constraint 阻擋另一個 Worker 啟動重複運算。

### annual_periods 語義：記錄用途，非計算用途

`config.yaml` 的 `annual_periods` 定義為**每年交易日數**（crypto=365, 台股=252）。

**實作決策**：`compute_all` 內部**不**做 `annual_periods × periods_per_day(timeframe)` 乘法（與 plan 原案不同）。改為完全委託 `_infer_annual_periods(ts_index)` 從實際資料密度推算 bars/year，傳給 QuantStats。

**原因**：inferred 值直接反映實際資料密度，能正確處理非 24h 交易的市場（如台股 5h/day）。手動乘法 `252 × 24 = 6048` 會高估台股的年化 bar 數。

`annual_periods` 目前用途：
1. 存入 DB `perf_params`，供 `check_existing_run` 判斷是否需要重算
2. `build_config` 從 `data_source` 推導預設值（crypto→365, tw→252）

### ddof 參數：佔位，尚未接入

`compute_all` 接受 `ddof` 但 QuantStats 的 `sharpe` / `sortino` 內部使用 `ddof=1`，不支援外部覆蓋。
目前 `ddof` 是 pass-through 佔位，存入 DB 但不影響計算。等未來自己實作 Sharpe/Sortino 時再接上。

### compute_signal_metrics 前視偏誤

`compute_signal_metrics` 計算 forward return 時必須嚴格對齊時間軸：
- Signal 在 $T$ 產生 → Forward Return = $Price_{T+k} / Price_{T+1\_open} - 1$
- **絕不能**用 $T$ 期的 Close 作為分母（那是產生 signal 的價格，不是 entry price）
- 須搭配 `fix_look_ahead_bias.md` 的 next-bar execution 邏輯

### 指標計算模組結構：不強制統一

策略 metrics（`compute_all`）和訊號 metrics（`compute_signal_metrics`）計算本質不同（equity curve + trades vs signal + price series），不合併為同一函式。共用的基礎統計工具（drawdown、annualize_return、rolling_sharpe）待 signal_metrics 需求明確後抽到 `librae/core/stats.py`：

```
librae/core/
├── stats.py              ← 共用 building blocks（延後）
├── metrics.py            ← 策略：compute_all(equity, trades, **perf_params)
└── signal_metrics.py     ← 訊號：compute_signal_metrics(data, cfg)（延後）
```

### SignalPoller 的適用範圍與未來擴展性

**Stateless 約束**：SignalPoller 內部沒有帳戶/部位狀態。傳入的 `feature_fn` / `prepare_signals`
必須是**純特徵工程、無部位依賴（portfolio-agnostic）**的 alpha 訊號。
若訊號邏輯依賴當前持倉狀態（如「有做多部位且跌破均線才觸發出場」），須使用 LiveTrader。

**未來擴展**：目前 SignalPoller 純收訊號，不計算 PnL。但架構上預留了擴展空間：
未來可在外層套一個 PortfolioOptimizer，將 signal-only 的 forward return
轉為「等權重無槓桿 PnL 基準」。SignalPoller 的乾淨介面（只輸出 signal_events + ohlcv）
正好是 optimizer 的輸入，不需要改動 SignalPoller 本身。

---

## 相關 Plan

- [`fix_look_ahead_bias.md`](fix_look_ahead_bias.md) — 前視偏誤修正（回測引擎 next-bar execution + Signal dashboard entry price）
  - **與本計劃的交互**：`fill_price` 的語意取決於 next-bar execution 是否落地。引擎改為 next-bar 後，`fill_price: open` 表示「訊號在 bar[i] 產生，bar[i+1] 的 Open 成交」（最接近實務）。`fill_price: close` 則為「bar[i+1] 的 Close 成交」（保守估計）。預設值從 `close` 改為 `open`，因為這是實務上最常見的執行方式。兩個計劃應同步實施，避免 fill_price 語意在過渡期混淆。
