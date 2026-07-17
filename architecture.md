# Architecture & Naming Conventions

> **文件定位**：這是一份**持續更新的現況文件**，反映系統目前的架構與命名慣例，隨程式碼演進直接修改本檔。
> 這與 `docs/decisions/`（決策當下存證、寫下後不回溯修改）性質相反 —— 本檔只承載「現在是什麼」，
> 命名規則背後「為什麼」的決策脈絡留在對應的 decision 文件，本檔用連結交叉引用。
>
> 新增/修改 table、column、`db/` 讀寫函數時，**必須同步更新本文件**。若命名規則本身改變（而非新增條目），
> 視情況在 `docs/decisions/` 補一份新的 decision 記錄「為什麼改」。

## 系統分層概覽

```
librae (core → backtest / live)  →  db (timescale_writer / timescale_reader)  →  Grafana / Streamlit
```

- `librae/core/`：策略執行的共用邏輯（`strategy.py` 定義 Position/Action/Fill，`executor.py` 定義 TradeResult/OrderEvent 與撮合邏輯），backtest 與 live 共用。
- `librae/backtest/engine.py`：逐 bar 回測引擎，產出 `BacktestOutput`（`librae/backtest/schema.py` 定義的 DB 持久化用 dataclass：RunMetadata/EquityCurvePoint/OrderEventRecord/StrategyMetrics）。
- `librae/live/engine.py`：sim/live 模式的即時輪詢引擎，同一份 executor 邏輯，即時寫入 DB。
- `db/timescale_writer.py` / `db/timescale_reader.py`：唯一的 DB 存取層，上層一律透過這裡讀寫，不直接下 SQL。
- Grafana（`app/grafana/generate_dashboards.py` 產生 JSON）與 Streamlit：下游視覺化，直接查詢 TimescaleDB。

分層細節與四層分離的決策脈絡見 `docs/decisions/2026-03-26-platform-architecture.md`（現況已用 librae 取代文件中提到的舊執行層）。

## 資料庫設計規範

### 資料表命名規則

| 類型 | 規則 | 範例 |
|---|---|---|
| 離散事件/紀錄表（每列代表一次獨立發生的事件或紀錄） | 複數 | `backtest_runs`, `trade_events`, `signal_events`, `ohlcv_coverage_ranges` |
| 代表連續時序整體的領域慣用詞（每列是整體序列的一個點，但表名指稱的是序列本身） | 維持領域單數慣用詞 | `equity_curve`, `ohlcv` |

### 時間戳記命名規則

**`ts` 只保留給 hypertable 的時間維度欄位**（`ohlcv`/`equity_curve`/`trade_events`/`signal_events` 的分區鍵，代表「這一列發生的時間」）。
**所有其他時間點中繼資料一律用 `_at` 後綴**，即使是作為查詢範圍過濾參數（例如 `load_ohlcv(started_at=..., ended_at=...)`）也一致套用，避免同一個字根在不同函式簽章裡時而叫 `ts` 時而叫別的名字。

| 欄位 | 意義 | 出現位置 |
|---|---|---|
| `started_at` | run 的資料區間起點 | `backtest_runs`, `RunMetadata`, `load_ohlcv()` 查詢參數 |
| `ended_at` | run 的資料區間終點 | 同上 |
| `run_at` | run 被執行/建立的時間 | `backtest_runs`, `RunMetadata` |
| `entry_at` | 部位進場時間 | `trade_events`, `Position`, `PositionState`, `TradeResult`, `OrderEvent`, `OrderEventRecord` |
| `exit_at` | 交易出場時間 | `TradeResult` |
| `last_heartbeat_at` | 執行程序最後一次回報存活的時間 | `backtest_runs` |
| `range_started_at` | 快取覆蓋區間起點 | `ohlcv_coverage_ranges` |
| `range_ended_at` | 快取覆蓋區間終點 | `ohlcv_coverage_ranges` |

### 現行 7 張表一覽

| 表名 | 用途 | PK / FK | Hypertable |
|---|---|---|---|
| `backtest_runs` | Run 中樞，1 row / run | PK `run_id` | 否 |
| `equity_curve` | 每 bar 淨值 | FK `run_id` → `backtest_runs` CASCADE | 是（`ts`） |
| `trade_events` | 部位生命週期事件（open/add/reduce/close） | FK `run_id`（nullable） | 是（`ts`） |
| `strategy_performance` | 聚合 KPI，1 row / run | PK+FK `run_id` → `backtest_runs` CASCADE | 否 |
| `ohlcv` | 共用市場資料 | 無 FK | 是（`ts`） |
| `signal_events` | 訊號品質監控（策略原始訊號，非成交紀錄） | FK `run_id`（nullable） | 是（`ts`） |
| `ohlcv_coverage_ranges` | `get_ohlcv()` 快取覆蓋區間追蹤（每列一個 range） | 無 FK | 否 |

### 數量歧義處理原則

同一筆紀錄若同時存在「本次成交量」與「事件後剩餘部位量」，禁止用 `quantity` 泛稱兩者 —— 名稱本身要能區分語意。統一用：

- `fill_quantity` — 本次事件的成交量
- `remaining_quantity` — 事件後剩餘部位量

**只在同時持有兩者的類別上做這個區分**（`trade_events` 表、`OrderEvent`、`OrderEventRecord`）。單一數量欄位的類別（`Position.quantity`、`PositionState.quantity`、`Fill.quantity`、`Action.quantity`、`TradeResult.quantity`）維持 `quantity` 不變 —— 它們本身沒有歧義，不需要比照修改。

### 純量計數不可用複數

代表「持有了幾根 bar」的整數計數統一用 `periods_held`，不用複數形式（複數容易誤讀成列表）。套用在所有代表這個概念的欄位/屬性上：`trade_events.periods_held`、`Position.periods_held`、`PositionState.periods_held`、`TradeResult.periods_held`、`OrderEvent.periods_held`、`OrderEventRecord.periods_held`。

### 報酬率命名

`period_return` / `benchmark_period_return`：每個 bar 的報酬率，不綁定特定頻率字眼（不用 `1d` 這類字根）—— `timeframe` 可以是 1h/4h/1d 任何頻率，命名不該暗示固定為日頻。

## Python 函數命名規範

### `db/timescale_writer.py`（五類動詞，寫在該檔案的 module docstring）

```
write_*   — 單表 INSERT/UPSERT（可包含型別/時區正規化），整列寫入
update_*  — 單表局部 UPDATE，只更新既有列的部分欄位
merge_*   — 單表 read-modify-write 整併邏輯（例如區間合併），超出單純 UPSERT 範圍
save_*    — 多表交易性協調器；可能從更廣的輸入中萃取/轉換資料
refresh_* — 從其他表重新計算衍生/聚合資料並 upsert 結果
```

判斷準則：**單表 vs 多表**決定 `write_`/`save_` 二選一；**整列寫入 vs 局部更新既有列**決定 `write_`/`update_`；**是否需要先讀取既有資料才能決定寫入內容**（而非單純 UPSERT）用 `merge_`；**是否從其他表重新聚合**用 `refresh_`。

例：`save_backtest_output`（一次寫 5 張表，多表協調器）、`write_trade_event`（單表整列寫入）、`update_heartbeat`（單表局部更新一個欄位）、`merge_ohlcv_coverage_ranges`（要先讀既有區間才能決定合併結果）、`refresh_performance`（從 `equity_curve` + `trade_events` 重新算 KPI 寫回 `strategy_performance`）。

### `db/timescale_reader.py`（三類動詞，寫在該檔案的 module docstring）

```
get_*    — 單一純量 / 小型物件查詢（id、dict、list of tuples）
load_*   — 回傳 DataFrame 的批次查詢，供分析/dashboard 使用
derive_* — 從既有資料算出不同形狀的結果；不是原始表的直接讀取
```

例：`get_run_by_config_hash`（回傳 dict）、`load_trade_events`（回傳 DataFrame）、`derive_trade_signals`（從 `trade_events`「反推」出進出場訊號序列，**不是**在讀 `signal_events` 表 —— 這兩者容易混淆，命名刻意用 `derive_` 而非 `load_` 來提醒呼叫端這是衍生資料，不是原始訊號）。

## 維護規則

1. 新增/修改 table、column，或 `db/timescale_writer.py`、`db/timescale_reader.py` 裡的讀寫函數時，同步更新本文件對應章節。
2. 新增欄位如果碰到「這個名字算不算歧義」「該不該用 `_at`」等邊界判斷，對照上面「數量歧義處理原則」「時間戳記命名規則」的準則，而不是逐案自行決定。
3. 若命名規則本身要改變（而非單純新增條目），在 `docs/decisions/` 開一份新的 decision 記錄改動原因，本檔案改完後只反映最終現況，不保留舊規則的說明。
