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

### 2. 引擎使用範例

#### 回測 (backtest)

```python
# --- utils.py: 資料準備（策略特有的 ETL） ---
def prepare_signals(h1_base, params=None):
    h1 = compute_features(h1_base, params)               # 技術指標
    h1["entry_signal"] = compute_entry_conditions(h1)     # 進場信號
    h1["exit_signal"] = compute_exit_conditions(h1)       # 出場信號
    return h1

# --- strategy.py: 決策邏輯 ---
class MyStrategy(BaseStrategy):
    def on_bar(self, ctx: Context) -> list[Action]:
        if ctx.positions.get(ctx.instrument):             # 有持倉 → 檢查出場
            if ctx.bar.get("exit_signal"):
                return [Action(type="close", instrument=ctx.instrument)]
            return []
        if ctx.bar.get("entry_signal"):                   # 無持倉 → 檢查進場
            return [Action(type="buy", instrument=ctx.instrument)]
        return []

# --- run.py: 串接引擎 ---
from librae import Backtest
from librae.backtest.persistence import save_output
from librae.config import get_market

df = fetch_and_prepare(symbol, months)                    # 1. ETL
market_config = get_market("crypto")
bt = Backtest(data=df, strategy=MyStrategy(), market_config=market_config)
bt.add_benchmark(df.xs(symbol, level="instrument")["close"])
bt.run()                                                  # 2. 跑引擎
output = bt.build_output(annualize=True)                  # 3. 指標 + 標準輸出
save_output(output, Path("data/backtests"))               # 4. 存檔
write_backtest_output(output)                             # 5. 寫 DB → Grafana
```

```bash
python -m strategies.trendpullback.run --mode backtest --symbol BTCUSDT --months 6
python -m strategies.trendpullback.run --config strategies/trendpullback/config.yaml --dry-run
```

#### 模擬監控 (sim)

```python
# --- run.py: 同一份策略，切換到 sim mode ---
from librae.live.wiring import build_live_trader

strategy = MyStrategy()
trader = build_live_trader(
    strategy=strategy,
    strategy_name="my_strategy",
    feature_fn=prepare_signals,         # 同一個 ETL pipeline
    symbols=["BTCUSDT"],
    timeframe="H1",                     # canonical label，wiring 內部用 to_ccxt() 轉換
    poll_interval=60,
)
trader.run()  # DB 寫入、Telegram、heartbeat、KPI 更新全由引擎處理
```

```bash
python -m strategies.trendpullback.run --mode sim --symbol BTCUSDT
```

**Docker 部署**：

```bash
cd deploy
cp .env.example .env
# 編輯 .env 填入 Telegram credentials（選填）

# 啟動 sim（支援多策略多標的同時跑）
./sim_start.sh trendpullback BTCUSDT          # 起 TrendPullback 監控 BTC
./sim_start.sh trendpullback_m5 BTCUSDT 30    # 同時起 M5 版本

# 停止
./sim_stop.sh trendpullback BTCUSDT           # 停止指定策略+標的
./sim_stop.sh --all                           # 停止所有 sim
```

腳本參數：`sim_start.sh <strategy> [symbol] [poll_interval]`

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `strategy` | （必填） | 策略名稱（對應 `strategies/<name>/run.py`） |
| `symbol` | `BTCUSDT` | 監控標的（多標的用逗號分隔） |
| `poll_interval` | `60` | 輪詢間隔（秒） |

Telegram 等環境變數從 `deploy/.env` 讀取。

**監控頻率**：sim service 每 `poll_interval` 秒檢查一次是否有新的完成 bar。策略時間框架決定實際信號觸發頻率（如 H1 策略每小時觸發一次）。Grafana Status panel 以 2 倍策略時間框架為閾值判斷 Online/Offline。

**查看結果**：Grafana → Strategy Dashboard → 選 mode=sim → 選 run_id。

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

## 架構

```
策略 ETL (strategies/*/utils.py) → DataFrame (MultiIndex + 信號欄位)
策略邏輯 (strategies/*/strategy.py) → on_bar(ctx) → Action[]
回測引擎 (librae/)               → Executor.execute(action) → Fill → Result
Sim 封裝 (librae/sim_wiring.py)  → DB callbacks + Telegram + heartbeat
CLI 共用 (librae/cli.py)         → base_parser + config YAML 載入
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
│   ├── live_runner.py      # LiveTrader polling loop
│   ├── sim_wiring.py       # Sim mode 基礎設施封裝
│   ├── cli.py              # 共用 CLI parser + config YAML 載入
│   ├── utils.py            # build_backtest_output, generate_run_id
│   ├── cost_model.py       # 成本模型（手續費 / 滑價 / 稅）
│   ├── metrics.py          # QuantStats adapter
│   ├── schema.py           # BacktestOutput, TradeRecord, StrategyMetrics
│   ├── notifications/      # Telegram 推播
│   └── config/             # markets.yaml（市場 / 標的設定）
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

## 常用指令

| 指令 | 說明 |
|------|------|
| `pytest tests/ -q` | 跑測試 |
| `python -m strategies.trendpullback.run --mode backtest --dry-run` | 快速回測 |
| `python -m strategies.trendpullback.run --mode sim` | 啟動模擬監控 |
| `python app/grafana/generate_dashboards.py` | 重新產生 Grafana JSON |
| `python deploy/setup_grafana.py` | 部署儀表板到 Grafana |

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
