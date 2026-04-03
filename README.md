# quant-strategy-lab

量化策略研究與即時監控平台。自建回測引擎 ([librae](librae/README.md)) + 策略框架 + TimescaleDB + Grafana。

---

## 架構

```
本機（開發 + 研究）                     VPS（Docker）
┌──────────────────┐                  ┌─────────────────────┐
│ 程式碼 + 回測     │                  │ TimescaleDB  :5432  │
│ 訊號研究          │──TIMESCALE_DSN─→│ Sim process         │
│ Grafana    :3000 │──HTTP query───→│ Signal monitor      │
└──────────────────┘                  └─────────────────────┘
```

- **TimescaleDB + Sim**：放 VPS，7x24 運行
- **Grafana**：本機開啟，連遠端 DB 查詢，幾乎不吃資源
- **回測 / 訊號研究**：本機跑，結果寫入遠端 DB

---

## Quick Start

```bash
# 安裝
git clone git@github-quant-strategy:awwesomeman/quant-strategy-lab.git
cd quant-strategy-lab
python3.12 -m venv .venv && .venv/bin/pip install -e .

# 設定 DB 連線（連遠端 VPS）
export TIMESCALE_DSN="postgresql://quant:password@your-vps-ip:5432/quant"

# VPS 端：啟動 DB（首次會自動建表）
cd deploy && docker compose up -d timescaledb

# 本機：產生 Grafana 儀表板
python -m app.grafana.generate_dashboards
```

---

## 兩種研究模式

### 訊號研究（不需要回測引擎）

評估一個指標的預測力，只需要 OHLCV + feature pipeline。

```
experiments/signals/kdj_oversold/
├── signal.py      # 純指標計算 + prepare_signals()
├── strategy.py    # HoldStrategy（sim 用，3 行）
└── run.py         # backtest / sim 入口
```

```bash
# 回測：跑歷史訊號 → 寫 DB → Grafana Signal Monitor 看結果
python -m experiments.signals.kdj_oversold.run

# 即時監控：每根 bar 寫入 signal_outcomes
python -m experiments.signals.kdj_oversold.run --sim
```

**Grafana**：Signal Monitor → `$mode=backtest` 或 `sim` → `$strategy=kdj_oversold`

**寫新訊號**：複製 `kdj_oversold/` 資料夾，改 `signal.py` 的指標計算和 `run.py` 的 config。

### 策略回測（需要回測引擎）

完整的進出場邏輯 + 部位管理 + 成本模擬。

```
strategies/trendpullback/
├── strategy.py    # BaseStrategy 子類 — on_bar() 決策邏輯
├── utils.py       # 特徵工程 + 進出場條件
├── run.py         # backtest / sim 入口
└── config.yaml    # 參數配置
```

```bash
# 回測
python -m strategies.trendpullback.run --mode backtest

# 即時監控（VPS 上跑）
python -m strategies.trendpullback.run --mode sim
```

**Grafana**：Strategy Dashboard → `$mode=backtest` 或 `sim` → `$run_id`

---

## 資料流

```
get_ohlcv()                    save_signal_results()       save_strategy_results()
  │                              │                            │
  ├→ DB 有資料 → 直接回傳        ├→ signal_outcomes           ├→ backtest_runs
  ├→ DB 缺口 → API 補齊 → DB    └→ ohlcv                    ├→ equity_curve
  └→ DB 不可用 → API fallback                                ├→ trade_blotter
                                                             ├→ strategy_performance
on_signal_outcome (sim 每 bar)                               ├→ signal_outcomes
  └→ signal_outcomes                                         └→ ohlcv
```

---

## DB Schema（6 張表）

| 表 | 用途 | 寫入時機 |
|---|---|---|
| `ohlcv` | 共享市場資料（cache） | `get_ohlcv()` 自動寫入 |
| `signal_outcomes` | 訊號發射記錄 | backtest: `save_signal_results()` / sim: `on_signal_outcome` |
| `backtest_runs` | Run metadata + params | `save_strategy_results()` |
| `equity_curve` | 每 bar 淨值 | 同上 |
| `trade_blotter` | 成交記錄 | 同上 |
| `strategy_performance` | 聚合 KPI | 同上 |

重建 DB：
```bash
psql -U quant -d quant -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
psql -U quant -d quant -f deploy/timescale_init.sql
```

---

## Grafana 儀表板

| Dashboard | 用途 | 切換變數 |
|---|---|---|
| **Signal Monitor** | 訊號預測力：forward return, MFE/MAE, cumulative return | `$mode`, `$strategy`, `$symbol`, `$timeframe`, `$data_source`, `$n`, `$k` |
| **Strategy Dashboard** | 策略績效：equity curve, drawdown, trades | `$mode`, `$run_id` |

兩個 dashboard 都由 `generate_dashboards.py` 生成：

```bash
python -m app.grafana.generate_dashboards
```

### 本機開發儀表板

本機只跑 Grafana，連遠端 VPS 的 TimescaleDB。

**啟動：**

```bash
cd deploy
export POSTGRES_PASSWORD=your-password
docker compose -f docker-compose.local.yml up -d
```

**設定 datasource 連到 VPS（每次重建 container 需執行一次）：**

```bash
# 等 Grafana 啟動完成
sleep 5

# 用 API 把 datasource URL 改成 VPS IP
curl -X PUT -u admin:admin -H "Content-Type: application/json" \
  "http://localhost:3000/api/datasources/uid/P40AE60E18F02DE32" \
  -d '{
    "name": "TimescaleDB",
    "uid": "P40AE60E18F02DE32",
    "type": "grafana-postgresql-datasource",
    "url": "your-vps-ip:5432",
    "database": "quant",
    "user": "quant",
    "secureJsonData": {"password": "your-password"},
    "jsonData": {"database":"quant","sslmode":"disable","postgresVersion":1600,"timescaledb":true},
    "isDefault": true,
    "access": "proxy"
  }'
```

> `docker restart` 不需要重跑（Grafana volume 會保留）。
> 只有 `docker compose down -v`（刪 volume）後才需要重新設定。

**開發流程：**

1. 修改 `generate_dashboards.py` 的 panel 定義
2. `python -m app.grafana.generate_dashboards` 重新生成 JSON
3. Grafana 每 30 秒自動重載，不需重啟 — 直接刷新瀏覽器看結果

### VPS 全套部署

VPS 上跑完整 docker-compose（DB + Grafana + Sim 在同一個 Docker network）：

```bash
cd deploy && docker compose up -d
```

此模式 datasource 自動連到 `quant_timescaledb:5432`（Docker 內部 hostname），不需額外設定。

---

## 基礎設施設定

### 環境變數

```bash
# 必填：DB 連線
export TIMESCALE_DSN="postgresql://quant:password@your-vps-ip:5432/quant"

# 選填：Telegram 通知
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
```

完整變數清單見 `.env.example`。

### VPS 部署

```bash
# 首次
cd deploy && cp ../.env.example .env  # 編輯填入密碼
docker compose up -d timescaledb

# 更新程式碼後
git pull && docker compose up -d --build

# 重建 DB（清除所有資料）
docker exec -i quant_timescaledb psql -U quant -d quant < ../deploy/timescale_init.sql
```

### Sim 服務管理

```bash
# 啟動策略 sim
./deploy/sim_start.sh trendpullback

# 啟動訊號 sim
python -m experiments.signals.kdj_oversold.run --sim

# 停止
./deploy/sim_stop.sh trendpullback
```

---

## 目錄結構

```
quant-strategy-lab/
├── librae/                 # 回測引擎框架 → 詳見 librae/README.md
├── data/
│   ├── ohlcv.py            # 統一 OHLCV 入口：get_ohlcv()（DB-first + API fallback）
│   └── binance.py          # Binance API fetcher（底層，被 ohlcv.py 委派）
├── db/
│   ├── timescale_writer.py # write_* (單表) + save_* (多表 orchestrator)
│   └── timescale_reader.py # load_* 查詢函式
├── strategies/             # 策略實作（需要回測引擎）
│   ├── trendpullback/
│   └── trendpullback_m5/
├── experiments/
│   └── signals/            # 訊號實驗（不需要回測引擎）
│       └── kdj_oversold/
├── app/grafana/            # Grafana 儀表板 generator
├── deploy/                 # Docker Compose + SQL + sim 腳本
├── tests/                  # pytest
└── docs/                   # 決策記錄 + 執行計劃 + 部署指南
```

---

## 連線與 Cache

### 確認 DB 連線

```bash
# 方法 1：用 Python（走跟程式碼相同的連線池）
python -c "from db import get_conn; c=get_conn().__enter__(); cur=c.cursor(); cur.execute('SELECT 1'); print('OK:', cur.fetchone())"

# 方法 2：用 psql 直連
psql "$TIMESCALE_DSN" -c "SELECT 1"
```

預設 DSN 為 `postgresql://quant:quant_secret@localhost:5432/quant`（`db/__init__.py`）。
遠端 DB 需設定 `TIMESCALE_DSN` 環境變數，或透過 SSH tunnel / Tailscale 映射到 localhost:5432。

### OHLCV Local Cache

從 Binance API 抓取的市場資料會 cache 在本機：

```
data/cache/{SYMBOL}_{INTERVAL}_{SOURCE}.parquet    # 例：data/cache/BTCUSDT_1h_binance_spot.parquet
```

- 格式：Parquet
- 過期策略：最新一筆資料超過 **6 小時**即視為 stale，重新從 API 拉取
- 定義在 `data/binance.py`（`_DEFAULT_CACHE_DIR`、`_CACHE_MAX_AGE`）

> 只有 Binance OHLCV 有 local cache。DB 寫入（signal_outcomes、equity_curve 等）無 cache，斷線會直接報錯。

---

## 常用指令

| 指令 | 說明 |
|------|------|
| `pytest tests/ -q` | 跑測試 |
| `python -m experiments.signals.kdj_oversold.run` | 訊號回測 |
| `python -m experiments.signals.kdj_oversold.run --sim` | 訊號即時監控 |
| `python -m strategies.trendpullback.run --mode backtest` | 策略回測 |
| `python -m strategies.trendpullback.run --mode sim` | 策略即時監控 |
| `python -m app.grafana.generate_dashboards` | 重新產生 Grafana JSON |

---

## 設定檔總覽

| 檔案 | 設定什麼 | 是否進 git |
|------|---------|-----------|
| `.env.example` → `.env` | secrets + DB 連線 | `.env.example` 進，`.env` 不進 |
| `librae/config/markets.yaml` | 市場成本參數 | ✅ |
| `strategies/*/config.yaml` | 策略參數 + 通知 | ✅ |
| `deploy/timescale_init.sql` | DB schema | ✅ |

---

## 相關文件

- [`librae/README.md`](librae/README.md) — 引擎架構、API、類型系統
- [`docs/plans/`](docs/plans/) — 執行計劃
- [`docs/decisions/`](docs/decisions/) — 架構決策記錄
- [`docs/guides/`](docs/guides/) — 部署指南
