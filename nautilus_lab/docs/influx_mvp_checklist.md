# InfluxDB + Grafana MVP 最短驗證清單

本清單用於快速確認：`signals.jsonl -> InfluxDB(strategy_signals) -> Grafana`。

## 0) 前置

- 已啟動 InfluxDB（預設 `http://localhost:8086`）
- 已有可寫入的 token（**不要寫死在程式**）
- 在專案根目錄：`/home/jasonpan_subscribe/.openclaw/workspace/nautilus_lab`

```bash
export INFLUX_URL="http://localhost:8086"
export INFLUX_ORG="quant_research"
export INFLUX_BUCKET="nautilus_signals"
export INFLUX_TOKEN="<your-token>"
```

## 1) 檢查輸入資料

範例檔案：`data/monitor/signals.jsonl`

```bash
wc -l data/monitor/signals.jsonl
head -n 5 data/monitor/signals.jsonl
```

## 2) 本地解析驗證（不寫入）

```bash
python scripts/etl/signals_to_influx.py --dry-run
```

預期看到：
- `[DRY-RUN] parsed points: N`
- stats（含 `total/parsed/skipped`）

## 3) 寫入 InfluxDB

```bash
python scripts/etl/signals_to_influx.py
```

預期看到：
- `[OK] wrote N points to measurement=strategy_signals ...`

## 4) 直接查詢 Influx（Flux）

在 InfluxDB Data Explorer 或 CLI 執行：

```flux
from(bucket: "nautilus_signals")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "strategy_signals")
  |> limit(n: 20)
```

若有資料列即代表寫入成功。

## 5) Grafana Data Source 設定（InfluxDB v2）

Grafana -> **Connections** -> **Add new connection** -> **InfluxDB**

- Query language: **Flux**
- URL: `http://localhost:8086`
- Organization: `quant_research`
- Token: `${INFLUX_TOKEN}` 對應的值
- Default Bucket: `nautilus_signals`

按 **Save & test**，看到成功訊息即完成連線。

## 6) Grafana 最小查詢驗證

Panel Query（Flux）示例：

```flux
from(bucket: "nautilus_signals")
  |> range(start: -6h)
  |> filter(fn: (r) => r._measurement == "strategy_signals")
  |> filter(fn: (r) => r._field == "signal_strength")
  |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
```

可看到時間序列即 MVP 資料管線驗證完成。
