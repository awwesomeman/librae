# 2026-03-28 — 策略資料夾結構規範 + 模組遷移

> 狀態：partially superseded — `run.py`/`strategy.py` 已合併為單一檔案（`strategy.py` 同時是
> `BaseStrategy` 子類 + CLI entrypoint），原因是 `librae.cli` 的共用 runner 已把 DB/Backtest/
> LiveTrader 的重依賴改成呼叫時才 import，不再需要靠拆檔案避免拖進 import chain；下方「三檔案」
> 敘述僅供歷史參考。
> 注記：strategies/<name>/ 結構已落地，monitoring/ 目錄已清除。`trendpullback` 因子驗證未過
> （見 strategies/experiments/trendpullback/report.md）已從 strategies/trendpullback/ 降級移至
> strategies/experiments/trendpullback/，不再是本規範的 production 範例——目前沒有任何策略通過
> 驗證進入 production，strategies/ 下只有 experiments/ 與 module/。

## 背景

目前策略相關邏輯散落在多個 top-level package：
- `strategies/` — 策略類別 + 信號計算
- `pipeline/` — 資料取得 + 特徵工程（與 signals.py 部分重複）
- `monitoring/` — 排程 + 信號監控（混了通用工具和策略特定邏輯）
- `experiments/` — 實驗腳本

問題：
1. 新增策略需要改多個資料夾，沒有統一規範
2. pipeline/ 的 core_features.py 和策略的 signals.py 功能重複
3. monitoring/ 混了兩套信號系統（新版 signal_monitor + 舊版 monitor_core）
4. 回測/監控/實盤三種模式的執行腳本沒有標準化

## 決策

### 1. 策略資料夾結構規範

每個 production 策略在 `strategies/` 下有自己的子資料夾，包含三個標準檔案：

```
strategies/<strategy_name>/
  strategy.py     # BaseStrategy 子類（純決策邏輯，不含 I/O）
  utils.py        # 資料取得、ETL、特徵工程、訊號預計算
  run.py          # 統一入口：--mode backtest|monitor|live
```

#### strategy.py
- 繼承 `librae.strategy.BaseStrategy`
- 只實作 `on_bar(ctx) -> list[Action]`
- 不做 I/O、不抓資料、不算指標
- 參數透過 `__init__` 傳入

#### utils.py
- `fetch_and_prepare(symbol, months, ...) -> pd.DataFrame`
  - 抓 OHLCV（呼叫 librae.data）
  - 特徵工程（EMA、ATR 等策略特定指標）
  - 訊號預計算（entry_signal、exit_signal）
  - 轉換為 MultiIndex DataFrame
- 純函數，可被 run.py 和測試共用

#### run.py
- 統一 CLI 入口，透過 `--mode` 切換執行模式：
  - `backtest`：串接 `Backtest` engine + `BacktestExecutor`
  - `monitor`：串接 `LiveExecutor(simulation=True)`，只出訊號不下單
  - `live`：串接 `LiveExecutor(simulation=False)`，真實下單
- 支援排程：`python -m strategies.trendpullback.run --mode monitor`
- 支援 `--dry-run`、`--mode`、`--no-db` 等 runtime flags（策略參數從 config.yaml 讀取）

### 2. pipeline/ 遷移

pipeline/ 混了通用和策略特定邏輯，拆分如下：

| 現有檔案 | 歸屬 | 目標位置 |
|---------|------|---------|
| `fetchers/binance_fetcher.py` | 通用資料取得 | `librae/data.py` |
| `features/cache_store.py` | 通用快取 | `librae/data.py` 或 `librae/cache.py` |
| `features/core_features.py` | `resample_ohlcv` 通用 / `add_trendpullback_features` 策略特定 | 通用 → `librae/data.py`；策略特定 → 刪除（signals.py 已取代） |
| `features/core_data_sources.py` | 通用 data adapter | `librae/data.py` |

遷移完成後刪除 `pipeline/` top-level package。

### 3. monitoring/ 遷移

monitoring/ 有兩套重疊系統：

**保留 & 遷移：**

| 檔案 | 性質 | 目標 |
|------|------|------|
| `signal_monitor.py` | 策略特定信號生成 | 整合進 `strategies/trendpullback/run.py --mode monitor` |
| `scheduler.py` | 排程 + DB 寫入 | 排程邏輯整合進 `run.py`；DB 寫入已有 `db/timescale_writer.py` |
| `telegram.py` | 通用通知 | `librae/notifications/telegram.py` |
| `utils_state.py` | 通用狀態管理 | 保留為 shared utility 或移入 librae |
| `utils_dedupe.py` | 信號去重 | 保留為 shared utility 或移入 librae |
| `utils_logging.py` | JSONL logging | 保留為 shared utility 或移入 librae |
| `profiles/*.json` | 策略 config | 移入對應策略資料夾 |
| `run_scheduler.sh` | 部署腳本 | `deploy/` |

**刪除：**

| 檔案 | 原因 |
|------|------|
| `monitor_core.py` | 舊版，自己重寫指標計算，被 signals.py 取代 |
| `monitor_run.py` | 舊版執行器，被 run.py --mode monitor 取代 |

遷移完成後刪除 `monitoring/` top-level package。

### 4. experiments/ 定位

- 研究中的策略實驗，每個子資料夾獨立可執行
- 驗證完畢 → 策略類別和信號搬進 `strategies/`
- 不被其他模組 import

## 最終目錄結構

```
librae/                     # 回測引擎核心
  data.py                   # fetch_ohlcv, resample_ohlcv, cache
  notifications/
    telegram.py

strategies/                 # production 策略（每個子資料夾遵循標準結構）
  trendpullback/
    strategy.py             # BaseStrategy 子類
    utils.py                # fetch + ETL + signals
    run.py                  # --mode backtest|monitor|live

  experiments/               # 策略研究實驗（獨立可執行，不被其他模組 import）
    trendpullback_btc/

db/                         # TimescaleDB 讀寫
app/                        # Streamlit UI
brokers/                    # 券商 API adapter
scripts/                    # 通用工具（seed、demo）
deploy/                     # Docker + 部署腳本
```

## 實作順序

1. 撰寫本 decision doc
2. pipeline/ → librae/data.py 遷移
3. strategies/trendpullback/ 補齊 utils.py + run.py
4. monitoring/ 遷移 + 刪除
5. 更新 tests、README、pyproject.toml
