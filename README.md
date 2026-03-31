# quant-strategy-lab

量化策略研究與即時監控平台。自建回測引擎 ([librae](librae/README.md)) + 策略框架 + TimescaleDB + Grafana。

---

## Quick Start

```bash
# 安裝
git clone git@github-quant-strategy:awwesomeman/quant-strategy-lab.git
cd quant-strategy-lab
python3.12 -m venv .venv && .venv/bin/pip install -e .

# 啟動基礎服務（DB schema 由 docker-entrypoint-initdb.d 自動初始化）
cd deploy && cp ../.env.example .env  # 依需求填入 Telegram / Tailscale 設定
docker compose up -d timescaledb grafana

# 部署 Grafana 儀表板（首次 / 更新後）
cd .. && python deploy/setup_grafana.py
```

---

## 策略開發流程

完整流程分三階段：**撰寫 → 回測 → 模擬監控**。

### 1. 撰寫策略

每個策略放在 `strategies/<name>/` 下，標準結構：

```
strategies/trendpullback/
├── strategy.py     # BaseStrategy 子類 — on_bar() 決策邏輯
├── utils.py        # 純函數：特徵工程 + 進出場條件計算
├── run.py          # CLI 入口：backtest / sim / live 三模式
└── config.yaml     # 預設參數（CLI 可覆蓋）
```

策略開發者負責三件事：**ETL（utils.py）、決策邏輯（strategy.py）、參數配置（config.yaml）**。
引擎負責其餘一切（持倉管理、成交模擬、績效計算、DB 寫入）。

### 2. 回測 + 模擬

引擎 API、類型系統、架構設計詳見 **[librae/README.md](librae/README.md)**。

```bash
# 回測（策略參數從 config.yaml 讀取）
python -m strategies.trendpullback.run --mode backtest
python -m strategies.trendpullback.run --mode backtest --dry-run

# 模擬監控
python -m strategies.trendpullback.run --mode sim
```

### 3. Docker 部署 sim

```bash
cd deploy
cp ../.env.example .env
# 編輯 .env 填入 Telegram credentials（選填）

# 啟動 sim（symbol, market 等從 config.yaml 讀取，poll_interval 可指定）
./sim_start.sh trendpullback          # 起 TrendPullback（預設 poll 60s）
./sim_start.sh trendpullback_m5 30    # 起 M5 版本（poll 30s）

# 停止
./sim_stop.sh trendpullback           # 停止指定策略
./sim_stop.sh --all                   # 停止所有 sim
```

腳本參數：`sim_start.sh <strategy> [poll_interval]`（預設 60s）。
策略的 symbol、market 等從 `strategies/<name>/config.yaml` 讀取。
Telegram 等 secrets 從 `deploy/.env` 讀取。

**監控頻率**：sim service 每 `poll_interval` 秒檢查一次是否有新的完成 bar。策略時間框架決定實際信號觸發頻率（如 H1 策略每小時觸發一次）。Grafana Status panel 以 2 倍策略時間框架為閾值判斷 Online/Offline。

**查看結果**：Grafana → Strategy Dashboard → 選 mode=sim → 選 run_id。

### 4. Heartbeat 監控

獨立於 sim 服務的外部監控腳本。查詢 DB 中所有 sim/live run 的 `last_heartbeat`，超過 3 倍 `poll_interval` 未更新就發 Telegram 告警。

```bash
# 一次性檢查
python deploy/check_heartbeat.py

# 持續監控（每 60 秒檢查一次）
python deploy/check_heartbeat.py --loop --interval 60

# cron（每 5 分鐘）
*/5 * * * * cd /path/to/quant-strategy-lab && .venv/bin/python deploy/check_heartbeat.py
```

需要 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID` 環境變數。

---

## VPS 部署 / 更新

### 首次部署

```bash
git clone git@github-quant-strategy:awwesomeman/quant-strategy-lab.git
cd quant-strategy-lab/deploy
cp ../.env.example .env   # 填入 Telegram / Tailscale 設定

docker compose up -d      # DB schema 由 initdb.d 自動初始化
cd .. && python deploy/setup_grafana.py
```

### 更新（拉新程式碼後）

```bash
cd quant-strategy-lab && git pull

# DB migration（如有新欄位，參考 deploy/timescale_init.sql 對比現有 schema）
docker exec -i quant_timescaledb psql -U quant -d quant <<'SQL'
ALTER TABLE <table> ADD COLUMN IF NOT EXISTS <column> <type>;
SQL

# 更新 Grafana + 重建 sim image
cd deploy && docker compose up -d --build
cd .. && python deploy/setup_grafana.py

# 確認 service 運行狀態
docker logs -f <container_name>
```

### 部分啟動

```bash
cd deploy

# 只啟動基礎服務（DB + Grafana）
docker compose up -d timescaledb grafana

# 加上 sim（用腳本）
./sim_start.sh trendpullback BTCUSDT
```

---

## 目錄結構

```
quant-strategy-lab/
├── librae/                 # 回測引擎框架 → 詳見 librae/README.md
├── data/                   # 資料取得（Binance OHLCV fetcher）
├── strategies/             # 策略實作
│   ├── trendpullback/      # H1 策略：D1 趨勢 + H1 回調進場
│   └── trendpullback_m5/   # M5 策略：M30 趨勢 + M5 回調進場（測試用）
├── pipeline/               # 資料取得 + ETL
├── brokers/                # 券商 adapter（Binance / Shioaji）
├── db/                     # TimescaleDB 讀寫
├── app/grafana/            # Grafana 儀表板 generator
├── deploy/                 # Docker Compose + SQL + sim 腳本
├── tests/                  # pytest（按模組分目錄）
└── docs/                   # 文件 + 架構決策記錄 (ADR)
```

---

## 設定檔總覽

本專案有 3 種設定檔，各自負責不同的事：

| 檔案 | 設定什麼 | 被誰讀取 | 是否進 git |
|------|---------|---------|-----------|
| `.env.example` → `.env.local` / `deploy/.env` | secrets + 基礎設施 | 環境變數（`os.environ`） | `.env.example` 進，`.env` 不進 |
| `librae/config/markets.yaml` | 市場成本參數 | `get_market()` → `MarketConfig` | ✅ |
| `strategies/*/config.yaml` | 策略參數 + 通知行為 | `parse_with_config()` → argparse + dataclass | ✅ |

### 1. 環境變數（`.env`）

存放 secrets 和基礎設施設定，**不進 git**。

```bash
# 本機開發
cp .env.example .env.local && source .env.local

# VPS / Docker
cp .env.example deploy/.env   # 編輯後由 sim_start.sh / docker-compose 讀取
```

完整變數清單見 `.env.example`：

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `TIMESCALE_DSN` | `postgresql://quant:quant_secret@localhost:5432/quant` | TimescaleDB 連線字串 |
| `POSTGRES_PASSWORD` | `quant_secret` | DB 密碼（docker-compose + Grafana datasource） |
| `GF_SECURITY_ADMIN_PASSWORD` | `admin` | Grafana admin 密碼 |
| `TELEGRAM_BOT_TOKEN` | （空） | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | （空） | Telegram chat/group ID |
| `TSDB_BIND` | `127.0.0.1` | TimescaleDB 對外綁定 IP（docker-compose only） |
| `TS_AUTHKEY` | （空） | Tailscale Auth Key（VPS only） |

**調用方式**：各模組直接用 `os.environ.get()` 或 `CredentialConfig.from_env(prefix)` 讀取。

### 2. 市場設定（`librae/config/markets.yaml`）

定義每個市場的成本模型參數，被引擎在回測/sim 啟動時讀取。

```yaml
# librae/config/markets.yaml
crypto:
  asset_class: crypto
  quote_currency: USDT
  commission_rate: 0.001      # 手續費率
  slippage_ticks: 2           # 滑價 tick 數
  tick_size: 0.01             # 最小價格變動
  multiplier: 1.0             # 合約乘數（現貨 = 1）
  transaction_tax: 0.0        # 交易稅
  # ...
```

新增市場只需在 `markets.yaml` 加一個區塊（已有 `crypto`、`tw_futures`、`us_equity`）。
程式碼調用方式見 [librae/README — Config API](librae/README.md#config-api)。

### 3. 策略設定（`strategies/*/config.yaml`）

每個策略的參數和通知偏好。只需寫跟預設值不同的部分。

**合併優先順序**：`config.yaml` → CLI args（CLI 最高優先）

```yaml
# strategies/trendpullback_m5/config.yaml
strategy:
  name: trendpullback_m5
  symbol: BTCUSDT
  market: crypto
  initial_balance: 100000
  timeframe: M5
  params:
    months: 1
    max_hold_bars: 24
    warmup_bars: 720

telegram:
  enabled: true
  notifications:
    status:
      enabled: true
```

`strategy:` 區塊包含策略定義（標的、市場、資金、timeframe）和演算法參數（`params`）。
`telegram:` 區塊控制通知行為。兩者以外沒有其他 YAML key。

**調用方式**：

```bash
# 自動載入同目錄的 config.yaml
python -m strategies.trendpullback_m5.run --mode sim
python -m strategies.trendpullback_m5.run --mode backtest --dry-run

# 指定其他設定檔
python -m strategies.trendpullback_m5.run --config path/to/other.yaml --mode sim
```

#### CLI 參數（僅 runtime flags）

| 參數 | 說明 |
|------|------|
| `--mode` | 執行模式：backtest / sim / live（預設 backtest） |
| `--config` | 指定設定檔（預設同目錄 config.yaml） |
| `--poll-interval` | sim 模式 poll 間隔秒數（預設 60） |
| `--dry-run` | 只跑不存檔 |
| `--no-db` | 跳過寫入 TimescaleDB |
| `--no-annualize` | 跳過年化指標計算 |

策略參數（symbol、market、timeframe、initial_balance、params.*）定義在 config.yaml 的 `strategy:` 區塊。

#### Telegram 通知設定（structured key）

寫在 config.yaml 的 `telegram:` 區塊，不經過 CLI。
bot_token / chat_id 從環境變數讀取，不放在 config.yaml。

```yaml
telegram:
  enabled: false              # 策略層開關
  # chat_id: "xxx"            # 覆蓋全域 env var（可選，不填用全域）
  notifications:
    signal: true              # 進出場信號
    startup: true             # 服務啟動/停止
    error: true               # 連續 poll 失敗告警
    status:
      enabled: false          # 定期狀態摘要
      interval_bars: 12       # 每 N 根 bar 發一次（M5×12=1h, H1×24=1d）
```

預設值定義在 `librae/config/notification.py` 的 dataclass。

---

## 常用指令

| 指令 | 說明 |
|------|------|
| `pytest tests/ -q` | 跑測試 |
| `python -m strategies.trendpullback.run --mode backtest --dry-run` | 快速回測 |
| `python -m strategies.trendpullback.run --mode sim` | 啟動模擬監控 |
| `python app/grafana/generate_dashboards.py` | 重新產生 Grafana JSON |
| `python deploy/setup_grafana.py` | 部署儀表板到 Grafana |

---

## 相關文件

- [`librae/README.md`](librae/README.md) — 引擎架構、API、類型系統
- [`docs/implementation_plan.md`](docs/implementation_plan.md) — 開發計劃與進度
- [`docs/decisions/`](docs/decisions/) — 架構決策記錄 (ADR)
- [`docs/learnings/`](docs/learnings/) — 開發筆記
