# quant-strategy-lab

量化策略研究與即時監控平台。自建回測引擎 (librae) + 策略框架 + TimescaleDB + Grafana。

---

## Quick Start

```bash
# 安裝
git clone git@github-quant-strategy:awwesomeman/quant-strategy-lab.git
cd quant-strategy-lab
python3.12 -m venv .venv && .venv/bin/pip install -e .

# 啟動基礎服務
cd deploy && cp .env.example .env  # 依需求填入 Telegram / Tailscale 設定
docker compose up -d timescaledb grafana

# 初始化 DB（首次）
docker exec -i quant_timescaledb psql -U quant -d quant < timescale_init.sql

# 部署 Grafana 儀表板（首次 / 更新後）
cd .. && python scripts/setup_grafana.py
```

---

## 策略開發流程

以 TrendPullback 為例，完整流程分三階段：**撰寫 → 回測 → 模擬監控**。

### 1. 撰寫策略

每個策略放在 `strategies/<name>/` 下，標準結構：

```
strategies/trendpullback/
├── strategy.py     # BaseStrategy 子類 — on_bar() 決策邏輯
├── utils.py        # 純函數：特徵工程 + 進出場條件計算
└── run.py          # CLI 入口：backtest / sim / live 三模式
```

**核心分工**：
- `utils.py`：計算技術指標 (EMA, ATR) 和進出場信號 (`entry_signal`, `exit_signal`)，純函數、無狀態。
- `strategy.py`：繼承 `BaseStrategy`，實作 `on_bar(ctx)`，根據信號欄位回傳 `Action`（buy / close / hold）。策略不追蹤持倉，持倉狀態由引擎管理。
- `run.py`：串接資料取得 → ETL → 策略 → 引擎 → 輸出。

### 2. 回測 (backtest)

```bash
# 快速驗證（不寫 DB）
python -m strategies.trendpullback.run --mode backtest --dry-run

# 完整回測：取 6 個月資料 → 跑引擎 → 存 JSON + 寫 DB
python -m strategies.trendpullback.run --mode backtest --symbol BTCUSDT --months 6

# 常用參數
#   --market crypto          市場類型（決定成本模型）
#   --initial-balance 100000 初始資金
#   --max-hold-bars 24       最大持倉 bar 數
#   --sample oos             樣本標記（in-sample / out-of-sample）
#   --no-annualize           不做年化（短期回測用）
#   --no-db                  不寫 TimescaleDB
```

回測結果寫入 TimescaleDB 後，開啟 Grafana → Strategy Dashboard → 選 mode=backtest 查看：
- 權益曲線 + Benchmark 對比
- KPI（Sharpe, MDD, Win Rate 等）
- 完整交易明細
- OHLCV + 進出場信號標記

### 3. 模擬監控 (sim)

Sim mode = paper trading：即時抓取市場資料 → 策略判斷 → 模擬成交 → 寫入 DB + Telegram 推播。

```bash
# 本地執行
python -m strategies.trendpullback.run --mode sim --symbol BTCUSDT

# 常用參數
#   --poll-interval 60       輪詢間隔（秒），預設 60s
#   --no-db                  不寫 DB（純測試）
```

**Docker 部署（推薦）**：

```bash
cd deploy
# .env 設定 Telegram 推播（選填）
# TELEGRAM_ENABLED=true
# TELEGRAM_BOT_TOKEN=<token>
# TELEGRAM_CHAT_ID=<chat_id>

docker compose up -d sim    # 啟動 sim service
docker logs -f quant_sim    # 查看運行狀態
```

Sim service 環境變數：

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `SIM_SYMBOL` | `BTCUSDT` | 監控標的 |
| `SIM_POLL_INTERVAL` | `60` | 輪詢間隔（秒） |
| `TELEGRAM_ENABLED` | `false` | 啟用 Telegram 推播 |

監控頻率說明：策略使用 H1 時間框架，sim service 每 `poll_interval` 秒檢查一次是否有新的小時 bar 完成。新 bar 完成時才會觸發策略計算和信號判斷。

Grafana → Strategy Dashboard → 選 mode=sim 查看即時數據。

---

## VPS 部署 / 更新

### 首次部署

```bash
git clone git@github-quant-strategy:awwesomeman/quant-strategy-lab.git
cd quant-strategy-lab/deploy
cp .env.example .env   # 填入 Telegram / Tailscale 設定

docker compose up -d
sleep 10
docker exec -i quant_timescaledb psql -U quant -d quant < timescale_init.sql
cd .. && python scripts/setup_grafana.py
```

### 更新（拉新程式碼後）

```bash
cd quant-strategy-lab && git pull

# DB migration（如有新欄位，參考 deploy/timescale_init.sql 對比現有 schema）
docker exec -i quant_timescaledb psql -U quant -d quant <<'SQL'
ALTER TABLE <table> ADD COLUMN IF NOT EXISTS <column> <type>;
SQL

# 重建有程式碼變動的 service + 更新 Grafana
cd deploy && docker compose up -d --build <service>
cd .. && python scripts/setup_grafana.py

# 確認 service 運行狀態
docker logs -f <container_name>
```

### 部分啟動

```bash
cd deploy

# 只啟動基礎服務（DB + Grafana）
docker compose up -d timescaledb grafana

# 加上 sim service
docker compose up -d sim
```

---

## 架構

```
ETL (pipeline/)        → DataFrame (MultiIndex + 信號欄位)
Strategy (strategies/) → on_bar(ctx) → Action[]
Engine (librae/)       → Executor.execute(action) → Fill → Result
```

**回測 vs 模擬 vs 實盤共用同一份策略，零修改**：

| | 回測 (backtest) | 模擬 (sim) | 實盤 (live) |
|---|---|---|---|
| 資料來源 | 歷史 OHLCV | 即時 OHLCV（polling） | 即時 OHLCV |
| 執行器 | BacktestExecutor | LiveExecutor(simulation=True) | LiveExecutor(simulation=False) |
| 下單 | 模擬成交 | 模擬成交 + Telegram 通知 | 真實下單（Phase 4） |

---

## 目錄結構

```
quant-strategy-lab/
├── librae/                 # 回測引擎（可獨立抽出）
│   ├── engine.py           # Backtest class
│   ├── strategy.py         # BaseStrategy ABC, Context, Action, Position
│   ├── executor.py         # BacktestExecutor + shared make_fill()
│   ├── live_executor.py    # LiveExecutor（sim / live）
│   ├── live_runner.py      # LiveRunner polling loop
│   ├── cost_model.py       # 成本模型（手續費 / 滑價 / 稅）
│   ├── metrics.py          # QuantStats adapter
│   ├── schema.py           # BacktestOutput, TradeRecord, StrategyMetrics
│   ├── notifications/      # Telegram 推播
│   └── config/             # markets.yaml（市場 / 標的設定）
├── strategies/             # 策略實作
│   └── trendpullback/      # strategy.py + utils.py + run.py
├── pipeline/               # 資料取得 + ETL
├── brokers/                # 券商 adapter（Binance / Shioaji）
├── db/                     # TimescaleDB 讀寫
├── app/grafana/            # Grafana 儀表板 generator
├── deploy/                 # Docker Compose + SQL + Dockerfile
├── tests/                  # pytest（按模組分目錄）
└── docs/                   # 文件 + 架構決策記錄 (ADR)
```

---

## 常用指令

| 指令 | 說明 |
|------|------|
| `pytest tests/ -q` | 跑測試 |
| `python -m strategies.trendpullback.run --mode backtest --dry-run` | 快速回測 |
| `python -m strategies.trendpullback.run --mode sim` | 啟動模擬監控 |
| `python app/grafana/generate_dashboards.py` | 重新產生 Grafana JSON |
| `python scripts/setup_grafana.py` | 部署儀表板到 Grafana |

---

## 環境變數

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `TIMESCALE_DSN` | `postgresql://quant:quant_secret@localhost:5432/quant` | TimescaleDB 連線 |
| `GF_SECURITY_ADMIN_PASSWORD` | `admin` | Grafana admin 密碼 |
| `TS_AUTHKEY` | （VPS 部署用） | Tailscale Auth Key |

---

## 相關文件

- [`docs/implementation_plan.md`](docs/implementation_plan.md) — 開發計劃與進度
- [`docs/decisions/`](docs/decisions/) — 架構決策記錄 (ADR)
- [`docs/learnings/`](docs/learnings/) — 開發筆記
