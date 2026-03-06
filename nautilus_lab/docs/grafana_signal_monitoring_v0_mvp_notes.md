# grafana_signal_monitoring_v0_mvp_notes

## dashboard_file_path
- `nautilus_lab/grafana/dashboards/signal_monitoring_dashboard_v0_mvp.json`

## panel_list
1. `signals_last_1h`  
   - 用途：最近 1 小時訊號量監控（異常放量/無訊號）  
   - measurement：`strategy_signals`（field: `signal_strength`，以 count 聚合）
2. `avg_signal_strength_last_24h`  
   - 用途：最近 24 小時訊號強度均值，判斷訊號品質是否退化  
   - measurement：`strategy_signals`（field: `signal_strength`）
3. `last_signal_age_minutes`  
   - 用途：最後一筆訊號距今多久，用於偵測訊號中斷  
   - measurement：`strategy_signals`（field: `signal_strength`，取 last `_time`）
4. `latest_win_rate_snapshot`  
   - 用途：補一張最小策略健康度快照（非完整績效）  
   - measurement：`strategy_performance`（field: `win_rate`）
5. `signal_strength_timeline`  
   - 用途：訊號強度時間序列（5m aggregate）  
   - measurement：`strategy_signals`（field: `signal_strength`）
6. `signal_count_5m`  
   - 用途：訊號頻率節奏（5m 計數）  
   - measurement：`strategy_signals`（field: `signal_strength`，以 count 聚合）
7. `recent_signals`  
   - 用途：最新 20 筆訊號列表（手機友善最小明細）  
   - measurement：`strategy_signals`（fields: `signal_strength`, `confidence`, `price`, `quantity`）
8. `signal_type_distribution_24h`  
   - 用途：近 24h 訊號型態分布  
   - measurement：`strategy_signals`（field: `signal_strength`，group by `signal_type`）

## variables
- 必備（依 contract）：
  - `schema_version`（constant hidden，預設 `1.0.0`）
  - `strategy`（query）
  - `run_id`（query，預設值文字保留 `latest`）
  - `sample`（custom：`train,oos`；預設 `oos`）
  - `timeframe`（query；預設 `H1`）
  - `asset`（query）
  - `side`（custom：`both,buy,sell`；預設 `both`）
  - `benchmark`（query；來自 `strategy_performance`）
- 額外（MVP 監控便利）：
  - `signal_type`（query；可快速篩選特定訊號類型）
  - `bucket`（constant hidden）
  - `influxdb_uid`（constant hidden，部署時替換）

## backend_fields_needed
以下為 dashboard 已先保留 query/欄位，但後端可能尚未穩定供給，需補齊：
1. `strategy_signals.sample`（tag，optional）
2. `strategy_signals.source`（tag，optional；便於來源稽核）
3. `strategy_signals.confidence`（field，optional）
4. `strategy_signals.price`（field，optional）
5. `strategy_signals.quantity`（field，optional）
6. `strategy_performance.benchmark`（tag required by contract，供 `benchmark` variable）
7. `strategy_performance.win_rate`（field required，供健康度 stat）
8. `strategy_signals.run_id` 的 `latest` 對應策略（目前 dashboard 以條件放寬處理；建議 backend 提供 latest run mapping 或 materialized view）

## naming_check
- 檔名、panel title、variables、field/tag keys 皆採 `snake_case`。
- key 未放單位；單位只在 Grafana unit 與註解層處理。
