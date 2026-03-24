# Grafana Schema Contract v0（MVP）

> ⚠️ 本文件為舊版草案；backend canonical schema 才是唯一來源：`docs/canonical_schema.yaml`。  
> 前端最新 mapping/blocker 請改看：`docs/frontend_schema_mapping.md`。  
> 目的：給 frontend（Grafana）直接對接 backend（Influx）使用的最小可用契約。  
> 對齊現有「策略回測分析」資訊架構：**策略總覽 / 交易明細**。  
> 設計原則：手機友善優先、欄位精簡、先保證監控與績效可讀性。

---

## 0) Contract Scope（v0）

- 時區：一律 `UTC`
- schema gate：所有 query 必須帶 `schema_version`
- sample：`train | oos`（v0 不開 validation）
- 命名：key 不帶單位；單位只在面板描述/欄位 metadata
- 版本：`schema_version = 1.0.0`

---

## 1) 面板 → 資料契約對照（measurement/tags/fields/time）

## A. 策略總覽（Summary）

### Panel A1：核心績效卡（Stat Card，手機 1 欄堆疊）
- 建議顯示：`trades`, `win_rate`, `avg_return_per_trade`, `avg_points_per_trade`, `profit_factor`, `equity_multiple`, `risk_reward_ratio`, `annual_return`, `annual_sharpe`, `annual_volatility`, `max_drawdown`
- measurement：`strategy_performance`
- time：run 結束快照時間（每 run 一筆）

**tags**
- 必須：`schema_version`, `strategy`, `asset`, `timeframe`, `run_id`, `sample`, `benchmark`
- 選配：`side`（long_only/short_only/both）、`market`

**fields**
- 必須：
  - `trades` (int)
  - `win_rate` (float, 0~1)
  - `avg_return_per_trade` (float)
  - `avg_points_per_trade` (float)
  - `profit_factor` (float)
  - `equity_multiple` (float)
  - `risk_reward_ratio` (float)
  - `annual_return` (float)
  - `annual_sharpe` (float)
  - `annual_volatility` (float)
  - `max_drawdown` (float, <=0)
- 選配：
  - `calmar_ratio` (float)
  - `sortino_ratio` (float)
  - `max_consecutive_losses` (int)
  - `payback_period_bars` (int)

---

### Panel A2：權益曲線 & 回撤（Time series）
- measurement：`perf_equity_curve`
- time：曲線時間序列點（固定頻率）

**tags**
- 必須：`schema_version`, `strategy`, `asset`, `timeframe`, `run_id`, `sample`, `benchmark`
- 選配：`side`

**fields**
- 必須：`equity`, `drawdown`, `benchmark_equity`
- 選配：`ret_1d`, `benchmark_ret_1d`

---

### Panel A3：訊號監控（Recent Signals）
- measurement：`strategy_signals`
- time：事件時間

**tags**
- 必須：`schema_version`, `strategy`, `asset`, `timeframe`, `run_id`, `side`, `signal_type`
- 選配：`source`, `sample`

**fields**
- 必須：`signal_strength`
- 選配：`confidence`, `price`, `quantity`

---

## B. 交易明細（Trade Details）

### Panel B1：交易列表（Table，手機預設精簡欄）
- measurement：`trade_blotter`
- time：成交/平倉時間（建議以 `exit_time` 為主時間）

**tags**
- 必須：`schema_version`, `strategy`, `asset`, `timeframe`, `run_id`, `sample`, `side`
- 選配：`exchange`, `session`, `entry_signal_type`, `exit_reason`

**fields**
- 必須：
  - `trade_id` (string-like, 用 field 存)
  - `entry_time_unix_ms` (int)
  - `exit_time_unix_ms` (int)
  - `entry_price` (float)
  - `exit_price` (float)
  - `qty` (float)
  - `holding_bars` (int)
  - `pnl` (float)
  - `pnl_points` (float)
  - `pnl_ratio` (float)
  - `commission` (float)
  - `slippage` (float)
- 選配：
  - `mae` (float)
  - `mfe` (float)
  - `risk_multiple_r` (float)
  - `comment_code` (string-like)

> 備註：Influx field 可接受字串，`trade_id` 保留為 field，便於 table 顯示與 drill-down。

---

### Panel B2：交易分布（Histogram/Bar）
- measurement：`trade_distribution`
- time：run 結束快照時間

**tags**
- 必須：`schema_version`, `strategy`, `asset`, `timeframe`, `run_id`, `sample`
- 選配：`side`

**fields**
- 必須：
  - `bucket_left` (float)
  - `bucket_right` (float)
  - `trade_count` (int)
- 選配：
  - `win_count` (int)
  - `loss_count` (int)

---

## 2) 命名規範（Naming Convention）

- 全部使用 `snake_case`。
- measurement 用名詞，不帶版本：如 `strategy_performance`。
- tag 表示切片維度（低基數優先），field 表示數值/明細。
- ID/類別優先放 tag（如 `strategy/run_id/sample/timeframe/asset`）。
- key 禁止單位尾碼（例如不要 `pnl_ntd`、`return_pct`）。
- 布林用 `is_` 前綴（v0 若無必要可不使用）。
- 比率一律用小數（0~1 或可負），呈現成百分比由前端決定。

---

## 3) 單位呈現規範（只在描述層，不在 key）

- 報酬/勝率/波動/回撤：資料存小數；Grafana 顯示 `%`。
- Sharpe/Sortino/Calmar/PF/R:R/EM：無單位。
- 價格：用交易標的報價單位（例如 index points）。
- `pnl`：以帳戶基礎貨幣（例如 TWD）。
- `pnl_points` / `avg_points_per_trade`：點數。
- 時間長度：
  - `holding_bars` 顯示 bars
  - timestamp 顯示本地時區可讀格式（資料仍存 UTC）

---

## 4) Dashboard 變數建議與預設策略

## 必備變數（依序）
1. `$strategy`
2. `$run_id`
3. `$sample`
4. `$timeframe`
5. `$asset`
6. `$side`
7. `$benchmark`
8. `$schema_version`

## 建議預設
- `$schema_version`：固定 `1.0.0`（hidden/constant）
- `$strategy`：`All`，排序後預設最近有資料者
- `$run_id`：預設 `latest`（query 取最新 `_time`）
- `$sample`：預設 `oos`
- `$timeframe`：預設 `H1`（若無則 fallback 第一個可用值）
- `$asset`：預設使用者最常看標的（可先 `All`）
- `$side`：預設 `both`
- `$benchmark`：預設策略 context 中 primary benchmark

## 行為建議（手機友善）
- 先鎖 `$strategy + $run_id + $sample`，其餘變數跟著連動縮小選單。
- 小螢幕預設只顯示：核心績效卡 + 權益曲線 + 最新 20 筆交易。

---

## 5) Frontend 可直接對接 Backend 的 JSON 契約（v0）

```json
{
  "schema_version": "1.0.0",
  "time_policy": {
    "timezone": "UTC",
    "precision": "ns",
    "query_gate_by_schema_version": true
  },
  "measurements": [
    {
      "name": "strategy_performance",
      "purpose": "策略總覽-核心績效",
      "time": "run_snapshot_time",
      "tags": {
        "required": ["schema_version", "strategy", "asset", "timeframe", "run_id", "sample", "benchmark"],
        "optional": ["side", "market"]
      },
      "fields": {
        "required": [
          {"name": "trades", "type": "int", "unit_desc": "count"},
          {"name": "win_rate", "type": "float", "unit_desc": "ratio_0_1"},
          {"name": "avg_return_per_trade", "type": "float", "unit_desc": "ratio"},
          {"name": "avg_points_per_trade", "type": "float", "unit_desc": "points"},
          {"name": "profit_factor", "type": "float", "unit_desc": "score"},
          {"name": "equity_multiple", "type": "float", "unit_desc": "score"},
          {"name": "risk_reward_ratio", "type": "float", "unit_desc": "score"},
          {"name": "annual_return", "type": "float", "unit_desc": "ratio"},
          {"name": "annual_sharpe", "type": "float", "unit_desc": "score"},
          {"name": "annual_volatility", "type": "float", "unit_desc": "ratio"},
          {"name": "max_drawdown", "type": "float", "unit_desc": "ratio_negative"}
        ],
        "optional": [
          {"name": "calmar_ratio", "type": "float", "unit_desc": "score"},
          {"name": "sortino_ratio", "type": "float", "unit_desc": "score"},
          {"name": "max_consecutive_losses", "type": "int", "unit_desc": "count"},
          {"name": "payback_period_bars", "type": "int", "unit_desc": "bars"}
        ]
      }
    },
    {
      "name": "perf_equity_curve",
      "purpose": "策略總覽-權益與回撤",
      "time": "curve_point_time",
      "tags": {
        "required": ["schema_version", "strategy", "asset", "timeframe", "run_id", "sample", "benchmark"],
        "optional": ["side"]
      },
      "fields": {
        "required": [
          {"name": "equity", "type": "float", "unit_desc": "index_or_nav"},
          {"name": "drawdown", "type": "float", "unit_desc": "ratio_negative"},
          {"name": "benchmark_equity", "type": "float", "unit_desc": "index_or_nav"}
        ],
        "optional": [
          {"name": "ret_1d", "type": "float", "unit_desc": "ratio"},
          {"name": "benchmark_ret_1d", "type": "float", "unit_desc": "ratio"}
        ]
      }
    },
    {
      "name": "strategy_signals",
      "purpose": "策略總覽-訊號監控",
      "time": "signal_event_time",
      "tags": {
        "required": ["schema_version", "strategy", "asset", "timeframe", "run_id", "side", "signal_type"],
        "optional": ["source", "sample"]
      },
      "fields": {
        "required": [
          {"name": "signal_strength", "type": "float", "unit_desc": "score"}
        ],
        "optional": [
          {"name": "confidence", "type": "float", "unit_desc": "ratio_0_1"},
          {"name": "price", "type": "float", "unit_desc": "quote_price"},
          {"name": "quantity", "type": "float", "unit_desc": "qty"}
        ]
      }
    },
    {
      "name": "trade_blotter",
      "purpose": "交易明細-逐筆交易",
      "time": "exit_time",
      "tags": {
        "required": ["schema_version", "strategy", "asset", "timeframe", "run_id", "sample", "side"],
        "optional": ["exchange", "session", "entry_signal_type", "exit_reason"]
      },
      "fields": {
        "required": [
          {"name": "trade_id", "type": "string", "unit_desc": "id"},
          {"name": "entry_time_unix_ms", "type": "int", "unit_desc": "epoch_ms"},
          {"name": "exit_time_unix_ms", "type": "int", "unit_desc": "epoch_ms"},
          {"name": "entry_price", "type": "float", "unit_desc": "quote_price"},
          {"name": "exit_price", "type": "float", "unit_desc": "quote_price"},
          {"name": "qty", "type": "float", "unit_desc": "qty"},
          {"name": "holding_bars", "type": "int", "unit_desc": "bars"},
          {"name": "pnl", "type": "float", "unit_desc": "base_ccy"},
          {"name": "pnl_points", "type": "float", "unit_desc": "points"},
          {"name": "pnl_ratio", "type": "float", "unit_desc": "ratio"},
          {"name": "commission", "type": "float", "unit_desc": "base_ccy"},
          {"name": "slippage", "type": "float", "unit_desc": "base_ccy"}
        ],
        "optional": [
          {"name": "mae", "type": "float", "unit_desc": "base_ccy_or_points"},
          {"name": "mfe", "type": "float", "unit_desc": "base_ccy_or_points"},
          {"name": "risk_multiple_r", "type": "float", "unit_desc": "score"},
          {"name": "comment_code", "type": "string", "unit_desc": "enum_code"}
        ]
      }
    },
    {
      "name": "trade_distribution",
      "purpose": "交易明細-損益分布",
      "time": "run_snapshot_time",
      "tags": {
        "required": ["schema_version", "strategy", "asset", "timeframe", "run_id", "sample"],
        "optional": ["side"]
      },
      "fields": {
        "required": [
          {"name": "bucket_left", "type": "float", "unit_desc": "ratio_or_points"},
          {"name": "bucket_right", "type": "float", "unit_desc": "ratio_or_points"},
          {"name": "trade_count", "type": "int", "unit_desc": "count"}
        ],
        "optional": [
          {"name": "win_count", "type": "int", "unit_desc": "count"},
          {"name": "loss_count", "type": "int", "unit_desc": "count"}
        ]
      }
    }
  ],
  "dashboard_variables": [
    {"name": "schema_version", "default": "1.0.0", "mode": "constant_hidden"},
    {"name": "strategy", "default": "All", "mode": "query"},
    {"name": "run_id", "default": "latest", "mode": "query"},
    {"name": "sample", "default": "oos", "mode": "custom", "options": ["train", "oos"]},
    {"name": "timeframe", "default": "H1", "mode": "query"},
    {"name": "asset", "default": "All", "mode": "query"},
    {"name": "side", "default": "both", "mode": "custom", "options": ["both", "buy", "sell"]},
    {"name": "benchmark", "default": "primary", "mode": "query"}
  ]
}
```

---

## 6) 與既有 canonical schema 的對齊說明

- `strategy_signals` / `strategy_performance` / `perf_equity_curve`：沿用既有 L1 合約概念。  
- 將 `symbol` 命名統一為 `asset`（frontend 展示語意更直覺）；若 backend 仍為 `symbol`，可在 query layer 做一次 mapping（僅 mapping，不做多重 alias）。
- 新增 `trade_blotter` / `trade_distribution` 作為「交易明細」MVP，欄位保持精簡，避免一次導入 L2/L3 全量事件流。

---

## 7) v0 非目標（先不做）

- Tick 級風控事件追蹤
- 訂單生命週期全事件（submit/ack/partial/cancel）
- 跨帳戶資金曲線聚合
- 多市場幣別即時換算

> 建議：先用本 v0 跑通 Grafana Dashboard 與資料寫入，再進到 v1 擴欄位。
