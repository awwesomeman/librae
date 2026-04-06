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

# 設定環境變數
cp .env.example .env   # 編輯 .env 填入 VPS_DB_HOST、POSTGRES_PASSWORD 等

# VPS 端：啟動 DB（首次會自動建表）
cd deploy && docker compose up -d timescaledb

# 本機：產生 Grafana 儀表板
python -m app.grafana.generate_dashboards
```

---

## 兩種研究模式

所有 runner 統一使用 `RunConfig` + `run_dispatch()` 模式：

```python
# run.py 標準結構
def run_backtest(cfg: RunConfig) -> None: ...
def run_realtime(cfg: RunConfig) -> None: ...

def main() -> None:
    from librae.cli import run_dispatch
    run_dispatch(STRATEGY_NAME, __file__, run_backtest, run_realtime)
```

config.yaml 定義參數，CLI 可覆蓋 (`--mode sim`, `--dry-run`, `--no-db` 等)。

### 訊號研究（不需要回測引擎）

評估一個指標的預測力，只需要 OHLCV + feature pipeline。

```
experiments/signals/kdj_oversold/
├── config.yaml    # 參數配置
├── utils.py       # 純指標計算 + prepare_signals()
└── run.py         # run_backtest / run_realtime 入口
```

```bash
# 回測：跑歷史訊號 → 寫 DB → Grafana Signal 看結果
python -m experiments.signals.kdj_oversold.run --mode backtest

# 即時監控：每根 bar 寫入 signal_events
python -m experiments.signals.kdj_oversold.run --mode sim
```

**Grafana**：Signal → `$mode=backtest` 或 `sim` → `$run_id`

**寫新訊號**：複製 `kdj_oversold/` 資料夾，改 `utils.py` 的指標計算和 `config.yaml` 的參數。

### 策略回測（需要回測引擎）

完整的進出場邏輯 + 部位管理 + 成本模擬。支援 long/short、同方向加碼（scaling）、部分平倉。
引擎使用 next-bar execution：bar[i] 產生決策，bar[i+1] 的價格成交，消除 look-ahead bias。

```
strategies/trendpullback/
├── strategy.py    # BaseStrategy 子類 — on_bar() 決策邏輯
├── utils.py       # 特徵工程 + 進出場條件
├── run.py         # run_backtest / run_realtime 入口
└── config.yaml    # 參數配置
```

```bash
# 回測
python -m strategies.trendpullback.run --mode backtest

# 即時監控（VPS 上跑）
python -m strategies.trendpullback.run --mode sim
```

**Grafana**：Strategy → `$mode=backtest` 或 `sim` → `$run_id`

---

## 資料流

```
get_ohlcv()                    save_signal_results()       save_strategy_results()
  │                              │                            │
  ├→ DB 有資料 → 直接回傳        ├→ signal_events            ├→ backtest_runs
  ├→ DB 缺口 → API 補齊 → DB    └→ ohlcv                    ├→ equity_curve
  └→ DB 不可用 → API fallback                                ├→ trade_events
                                                             ├→ strategy_performance
LiveTrader callbacks                                         ├→ signal_events
  ├→ on_order_event   → trade_events                         └→ ohlcv
  ├→ on_signal_outcome → signal_events
  ├→ on_bar            → equity_curve
  └→ on_ohlcv          → ohlcv
```

---

## DB Schema（6 張表）

| 表 | 用途 | 寫入時機 |
|---|---|---|
| `ohlcv` | 共享市場資料（cache） | `get_ohlcv()` 自動寫入 |
| `signal_events` | 訊號發射記錄 | backtest: `save_signal_results()` / sim: `on_signal_event` |
| `backtest_runs` | Run metadata + params + config_hash | `save_strategy_results()` |
| `equity_curve` | 每 period 淨值 | 同上 |
| `trade_events` | 部位生命週期事件（open/add/reduce/close） | 同上 |
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
| **Signal** | 訊號預測力：forward return, MFE/MAE, cumulative return | `$mode`, `$run_id`, `$n`, `$k` |
| **Strategy** | 策略績效：equity curve, drawdown, trades | `$mode`, `$run_id` |

兩個 dashboard 都由 `generate_dashboards.py` 生成：

```bash
python -m app.grafana.generate_dashboards
```

### 本機開發儀表板

本機只跑 Grafana，透過 `.env` 中的 `VPS_DB_HOST` 連遠端 TimescaleDB。

**啟動：**

```bash
# 確認 .env 已設定 VPS_DB_HOST=你的VPS-IP
cd deploy
docker compose -f docker-compose.local.yml up -d
```

Datasource 由 provisioning 自動設定（讀 `VPS_DB_HOST` 環境變數），不需手動用 API 改。

**開發流程：**

1. 修改 `generate_dashboards.py` 的 panel 定義
2. `python -m app.grafana.generate_dashboards` 重新生成 JSON
3. Grafana 每 30 秒自動重載，不需重啟 — 直接刷新瀏覽器看結果

### VPS 全套部署

VPS 上跑完整 docker-compose（DB + Grafana + Sim 在同一個 Docker network）：

```bash
cd deploy && docker compose up -d
```

VPS 的 `VPS_DB_HOST` 預設為 `quant_timescaledb`（Docker 內部 hostname），不需額外設定。

---

## 基礎設施設定

### 環境變數

所有環境變數統一放在專案根目錄的 `.env`（從 `.env.example` 複製）。
Docker Compose 和 shell 腳本都從同一份 `.env` 讀取。

完整變數清單見 `.env.example`。

### VPS 部署

```bash
# 首次
cp .env.example .env  # 編輯填入密碼、TIMESCALE_DSN 等
cd deploy && docker compose up -d

# 更新程式碼後
git pull && docker compose up -d --build

# 重建 DB（清除所有資料）
docker exec -i quant_timescaledb psql -U quant -d quant < timescale_init.sql
```

### Sim 服務管理

```bash
# 啟動策略 sim
./deploy/sim_start.sh trendpullback

# 啟動訊號 sim
python -m experiments.signals.kdj_oversold.run --mode sim

# 停止
./deploy/sim_stop.sh trendpullback
```

---

## 目錄結構

```
quant-strategy-lab/
├── librae/                 # 回測引擎框架 → 詳見 librae/README.md
│   ├── core/               #   RunConfig、策略協議、執行器、成本模型、指標
│   ├── backtest/           #   回測引擎（next-bar execution）
│   ├── live/               #   即時引擎（LiveTrader + LiveExecutor + SignalPoller）
│   ├── config/             #   市場設定、通知設定
│   ├── notifications/      #   Telegram 推播
│   └── cli.py              #   run_dispatch + build_config 入口
├── data/
│   ├── ohlcv.py            # 統一 OHLCV 入口：get_ohlcv()（DB-first + API fallback）
│   ├── binance.py          # Binance 公開 API fetcher（不需認證）
│   └── utils.py            # 共用工具：resample_ohlcv, parse_dt
├── brokers/
│   ├── crypto_adapter.py   # CCXT adapter（Binance/OKX/Bybit，需認證）
│   └── shioaji_adapter.py  # 永豐 Shioaji adapter（台灣期貨/股票，需認證）
├── db/
│   ├── timescale_writer.py # write_* (單表) + save_* (多表 orchestrator)
│   └── timescale_reader.py # load_* 查詢函式
├── strategies/             # 正式策略
│   ├── utils.py            # 共用特徵工具：merge_htf_column
│   ├── trendpullback/      # H1 趨勢回踩策略
│   └── trendpullback_m5/   # M5 變體
├── experiments/            # 實驗性策略與訊號研究
│   ├── signals/            #   訊號研究（kdj_oversold 等）
│   └── strategies/         #   實驗策略（trendmaster 等）
├── app/grafana/            # Grafana 儀表板 generator
├── deploy/                 # Docker Compose + SQL + sim 腳本
├── scripts/                # 開發 / 運維工具腳本
├── tests/                  # pytest
└── docs/                   # 決策記錄 + 執行計劃 + 部署指南
```

---

## 資料來源

| 層 | 用途 | 認證 |
|---|---|---|
| `data/` | 公開市場資料（OHLCV）— Binance REST API | 不需要 |
| `brokers/` | 需認證交易所 — 即時資料 + 下單 | 需要 API key |

`data/ohlcv.py` 是統一入口，支援 fetcher 註冊機制。
公開資料源（Binance）內建；需認證的（Shioaji）在 strategy 的 `run.py` 中註冊：

```python
from brokers.shioaji_adapter import ShioajiAdapter
from data.ohlcv import register_ohlcv_fetcher

adapter = ShioajiAdapter()
register_ohlcv_fetcher("shioaji", adapter.fetch_ohlcv)
df = get_ohlcv("TXFR1", "5m", periods=1, source="shioaji")
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

> 只有 Binance OHLCV 有 local cache。DB 寫入（signal_events、equity_curve 等）無 cache，斷線會直接報錯。

---

## 常用指令

| 指令 | 說明 |
|------|------|
| `pytest tests/ -q` | 跑測試 |
| `python -m experiments.signals.kdj_oversold.run --mode backtest` | 訊號回測 |
| `python -m experiments.signals.kdj_oversold.run --mode sim` | 訊號即時監控 |
| `python -m strategies.trendpullback.run --mode backtest` | 策略回測 |
| `python -m strategies.trendpullback.run --mode sim` | 策略即時監控 |
| `python -m app.grafana.generate_dashboards` | 重新產生 Grafana JSON |

---

## 設定檔總覽

| 檔案 | 設定什麼 | 是否進 git |
|------|---------|-----------|
| `.env.example` → `.env`（專案根目錄） | secrets + DB 連線 + Grafana + Telegram | `.env.example` 進，`.env` 不進 |
| `librae/config/markets.yaml` | 市場成本參數 | yes |
| `strategies/*/config.yaml` | 策略參數 + 通知 | yes |
| `experiments/*/config.yaml` | 實驗參數 | yes |
| `deploy/timescale_init.sql` | DB schema | yes |

---

## 相關文件

- [`librae/README.md`](librae/README.md) — 引擎架構、API、類型系統
- [`docs/plans/`](docs/plans/) — 執行計劃
- [`docs/decisions/`](docs/decisions/) — 架構決策記錄
- [`docs/guides/`](docs/guides/) — 部署指南
