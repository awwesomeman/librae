# L1 Data Contract（最小可用版）

> 範圍僅限 L1 儀表板：提供穩定展示「訊號、績效快照、權益曲線」資料。  
> 不含 L2/L3（交易明細、風控明細、事件追蹤）。

## Canonical Schema Owner（單一真源）

- Canonical schema 檔案：`docs/canonical_schema.yaml`
- 版本：`schema_version=1.0.0`
- **後端定義並維護 schema**；資料寫入與查詢契約必須遵循此單一真源，不使用 alias/fallback。
- 共用驗證 guard：`nautilus_lab/contracts.py`（前後端共用欄位與 key 驗證）。
- 前端啟動時必須以 `schema_version` 做查詢 gate；不符合版本的資料不得混入報表。

## 1) Measurement: `strategy_signals`

用途：L1 顯示近期策略訊號、方向、訊號強度與置信度。

### Tags（索引維度）
- `strategy`：策略名稱/ID（例：`DemoBreakout_v1.0`）
- `symbol`：標的（例：`TXF`）
- `timeframe`：訊號週期（例：`H1`）
- `side`：`buy|sell|hold`
- `source`：資料來源（例：`jsonl`）
- `run_id`：一次回測/跑批識別
- `signal_type`：`entry|exit|rebalance`（可擴充）

### Fields（數值）
- `signal_strength` (float, required)
- `confidence` (float, optional)
- `price` (float, optional)
- `quantity` (float, optional)

### Time
- `timestamp`（事件時間，建議 UTC）

---

## 2) Measurement: `strategy_performance`

用途：L1 顯示每次 run 的 train/oos 摘要績效卡。

### Tags
- `strategy`
- `symbol`
- `timeframe`
- `run_id`
- `sample`：`train|oos`
- `benchmark`：基準名稱（例：`TWSE`、`BTCUSDT`）

### Fields
- `total_return` (float)
- `annual_return` (float)
- `sharpe` (float)
- `max_drawdown` (float, 負值)
- `win_rate` (float, 0~1)
- `trades` (int)

### Time
- 寫入快照時間（run 結束時間）

---

## 3) Measurement: `perf_equity_curve`

用途：L1 畫權益曲線與 benchmark 對照。

### Tags
- `strategy`
- `symbol`
- `timeframe`
- `run_id`
- `sample`：`train|oos`
- `benchmark`

### Fields
- `equity` (float)
- `ret_1d` (float)
- `drawdown` (float, 負值)
- `benchmark_equity` (float)
- `benchmark_ret_1d` (float)

### Time
- 曲線時間序列點（建議固定頻率，如日頻/小時頻）

---

## Retention / Bucket 建議（L1 專用）

若先用單一 bucket（如 `nautilus_signals`）：
- L1 最小可用：保留 **180 天**（可支撐近 3~6 個月檢視）

若可拆 bucket：
- `l1_hot`（`strategy_signals`, `perf_equity_curve`）：**90~180 天**
- `l1_snapshot`（`strategy_performance`）：**365 天**

> 先求穩定可用，不必過早做多層保留策略。等 L2/L3 上線再細分 cold storage。

---

## 最小一致性規則

1. `run_id`、`sample`、`benchmark` 必須在績效相關 measurement 都存在。  
2. `sample` 僅允許 `train|oos`。  
3. 所有時間欄位統一 UTC。  
4. 缺值處理：
   - `signal_strength` 缺值時依 side 補值：buy=1, sell=-1, hold=0
   - 其他欄位缺值可省略，不寫 NaN 字串。
5. 儀表板查詢建議以 `strategy + symbol + timeframe + run_id` 為主鍵組合。
