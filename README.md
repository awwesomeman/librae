# quant-strategy-lab

量化策略研究與即時監控平台。自建回測引擎 ([librae](librae/README.md)) + 策略框架 + TimescaleDB + Grafana。

---

## Quick Start（本機）

```bash
git clone git@github-quant-strategy:awwesomeman/quant-strategy-lab.git
cd quant-strategy-lab
uv sync --extra test   # 開發/測試用；只要跑本機 tw_futures live 才需要再加 --extra tw-live

cp .env.example .env   # 填入 TIMESCALE_DSN、密碼
cp .env.secrets.example .env.secrets   # 若要跑 Shioaji 再填 SHIOAJI_*（這份不會被任何 deploy 腳本同步出去）
```

之後所有指令都透過 `uv run` 執行（例如 `uv run pytest tests/ -q`），或 `source .venv/bin/activate` 後直接跑——下面「常用指令」為求簡潔省略了 `uv run` 前綴。

---

## 策略開發流程

所有 runner 統一用 `RunConfig` + `run_dispatch()`：

```python
# strategy.py 標準結構（BaseStrategy 子類 + CLI 進場點，同一個檔案——見
# docs/decisions/2026-03-28-strategy-folder-convention.md 的 superseded 注記）
def run_backtest(cfg: RunConfig) -> None: ...
def run_realtime(cfg: RunConfig) -> None: ...

def main() -> None:
    from librae.cli import run_dispatch
    run_dispatch(STRATEGY_NAME, __file__, run_backtest, run_realtime)
```

`config.yaml` 定義參數，CLI 可覆蓋（`--mode`, `--dry-run`, `--no-db`, `--poll-seconds` 等）。

**新增策略**：先在 `strategies/experiments/<name>/` 寫 `factor_research.py`（因子驗證，寫法參考
`strategies/experiments/` 底下任何一個已經在跑 `factor_research.py` 的家族），**驗證通過才**建立
`strategy.py`（決策邏輯 + CLI 進場點）、`utils.py`（特徵 + 訊號）、`config.yaml`（參數），搬到
`strategies/<name>/`——目前哪些家族驗證過、通過與否，見 `strategies/FACTOR_ANALYSIS.md`（唯一的
current-state 索引，本文件不重複記錄，避免每次策略搬動都要回來改這裡）。`config.yaml` 通常不用寫
`market`/`data_source`——只要 symbol 已經在 `librae/config/symbols.yaml` 登記，就會自動解析（登記了
還手動寫且兩邊對不上會直接報錯，避免兩份設定悄悄分歧）；沒登記的一次性實驗 symbol，才在 `config.yaml`
顯式指定。`market: tw_futures` 會自動走 Shioaji，`market: crypto` 走 ccxt——不需要自己組 adapter。

**訊號研究**（不需要完整回測引擎，只評估指標預測力）：多數實驗是獨立的 `factor_research.py` 腳本，不掛 `RunConfig`/`config.yaml`，複製一份改指標邏輯即可；少數走完整 `run.py`/`config.yaml` 模式（哪些是哪種、目前是否可執行，見 `strategies/README.md`）。`strategies/experiments/` 不分子資料夾，一個資料夾一個探索過的想法。

---

## 策略回測

```bash
python -m strategies.<name>.strategy --mode backtest
```

- 引擎用 next-bar execution：bar[i] 產生決策、bar[i+1] 的價格成交，避免 look-ahead bias。
- `get_ohlcv()` 是 DB-first + API fallback：資料庫有就直接讀，缺口才補打 API 再寫回 DB（cache 依 symbol/timeframe/data_source 追蹤覆蓋區間，不會整段重抓）。
- 支援 long/short、同方向加碼（scaling）、部分平倉。
- 結果（`config_hash`/`params`/`start`~`end` 都存進 `backtest_runs`）+ 完整 equity/trade 明細寫入 DB，Grafana 用 `$run_id` 切換查看。

crypto（`binance_spot`）跟 Shioaji（`tw_futures`）都已經註冊好 `get_ohlcv` 的 fetcher，`get_ohlcv("TXFR1", ..., data_source="shioaji")` 可以直接回測，跟 crypto 走同一套 DB-first 快取。Shioaji 這邊需要本機有 `SHIOAJI_API_KEY`/`SHIOAJI_SECRET_KEY`（唯讀權限即可，不需要 CA）；fetcher 內部固定用 `simulation=True` 登入（歷史資料查詢不是下單，不需要正式權限，某些 key 甚至只有模擬權限能登入，細節見 `docs/learnings/ERRORS.md`）。

**非價量因子**（資金費率、未平倉量等有外部抓取成本的第三方資料）走 `strategies/module/data/factors.py` 的 `get_factor()`，跟 `get_ohlcv()` 同一套 DB-first + 缺口追蹤設計，只是共用一張 long table（`external_factors`）而不是每個資料源一張表，新增資料源只要註冊一個 fetcher（見 `factors.py` docstring），不用寫 migration。已有 `funding.py`（funding rate）、`open_interest.py`（未平倉量）兩個範例；`cross_asset.py`/`regime.py` 是從已快取的 OHLCV 現算的衍生特徵，不走這套快取（隨時能重算，沒有 gap 問題）。

---

## 策略模擬 / 實盤（sim & live）

```bash
# sim：本地 bookkeeping、不下真實單，Telegram 照樣推播訊號
python -m strategies.<name>.strategy --mode sim --poll-seconds 60

# live：市場資料/下單 adapter 皆自動從 env 建立（crypto: BINANCE_*，tw_futures: SHIOAJI_*）
python -m strategies.<name>.strategy --mode live --poll-seconds 60
```

- **`--poll-seconds` 必填**，sim/live 都沒有隱性預設值——要自己設成貼近策略 `timeframe` 的秒數（例如 M5 策略設 60s 內，太大會漏抓完成的 K 棒；太小則浪費 API 呼叫）。沒設會直接報錯，不會偷偷用舊的 60 秒。
- 兩種市場都是**輪詢（poll）＋比對最後一根 K 棒時間戳**的 bar-driven 設計，不是 WebSocket/snapshot 訂閱：crypto 打 ccxt `fetch_ohlcv`（Binance REST `/klines`），Shioaji 打 `kbars()`——兩邊資料源跟 backtest 一致，才不會出現「backtest 賺錢、live 對不上」的落差。
- `market: tw_futures` 時 `LiveTrader` 自動建立已認證的 `ShioajiAdapter`；否則（crypto）自動建立帶 `BINANCE_*` credentials 的 `CryptoAdapter`——兩者都同時當市場資料來源和下單通道，credentials 放哪見下方「設定檔總覽」。之後加第二個 crypto 交易所，只需換一個 prefix（例如 `OKX_*`），不用改共用邏輯。
- 常駐在 VM 上跑：見 [`architecture.md`「VM 部署與策略管理」](architecture.md#vm-部署與策略管理)，`./deploy/trade.sh start <name> sim 60` / `./deploy/trade.sh start <name> live 60`，停用 `./deploy/trade.sh stop <name> [sim|live]`。
- 第一次要對某個 symbol 跑 `live` 之前，先讀 [`architecture.md`「mode 與 sandbox」](architecture.md#modesimlive-vs-sandbox測試網模擬環境)：`mode`（策略要不要真的送單）跟 `sandbox`（送到測試網還是正式站）是兩個獨立開關，`live` + `BINANCE_SANDBOX=true`/`SHIOAJI_SANDBOX=true` 可以安全演練整條下單路徑，不動真錢。
- 掛掉偵測：`scripts/check_heartbeat.py --loop`，`backtest_runs.last_heartbeat` 超過 `3 × poll_seconds` 沒更新就用 Telegram 告警。

---

## 資料流 & DB Schema

見 [`architecture.md`「資料流」](architecture.md#資料流)、[「現行 9 張表一覽」](architecture.md#現行-9-張表一覽)。

重建 DB：
```bash
psql "$TIMESCALE_DSN" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
psql "$TIMESCALE_DSN" -f deploy/timescale_init.sql
```

---

## Grafana 儀表板

| Dashboard | 用途 | 切換變數 |
|---|---|---|
| **Signal** | 訊號預測力：forward return, MFE/MAE, cumulative return | `$mode`, `$run_id`, `$n`, `$k` |
| **Strategy** | 策略績效：equity curve, drawdown, trades | `$mode`, `$run_id` |

兩個 dashboard 都由以下指令生成（改完 `app/grafana/generate_dashboards.py` 的 panel 定義後重跑，Grafana 每 30 秒自動 reload，不需重啟)：

```bash
python -m app.grafana.generate_dashboards
```

本機開發用（只跑 Grafana，連遠端 DB）：
```bash
cd deploy && docker compose -f docker-compose.local.yml up -d
```

---

## 常用指令

| 指令 | 說明 |
|------|------|
| `pytest tests/ -q` | 跑測試 |
| `python -m strategies.<name>.strategy --mode backtest` | 策略回測（`<name>` 需要有已驗證通過的 `strategy.py`——目前有哪些，見 `strategies/FACTOR_ANALYSIS.md`） |
| `python -m strategies.<name>.strategy --mode sim --poll-seconds 60` | 策略模擬（不下真單） |
| `python -m strategies.<name>.strategy --mode live --poll-seconds 60` | 策略實盤 |
| `./deploy/build_push.sh` | 本機 build + push trade image（策略程式碼改了才需要） |
| `./deploy/trade.sh start <name> sim 60` / `trade.sh stop <name> sim` | 啟停常駐 sim 容器（本機或 VM 上執行皆可） |
| `./deploy/trade.sh start <name> live 60` / `trade.sh stop <name> live` | 啟停常駐 live 容器（真實下單，crypto/tw_futures 皆可） |
| `python scripts/check_heartbeat.py --loop` | 監控 sim/live 是否掛掉 |
| `python -m app.grafana.generate_dashboards` | 重新產生 Grafana JSON |

---

## 設定檔總覽

| 檔案 | 設定什麼 | 是否進 git |
|------|---------|-----------|
| `.env.example` → `.env`（專案根目錄） | DB 連線 + Grafana + Telegram + 非敏感設定；會被 `cloud_deploy.sh` 同步到 VM | `.env.example` 進，`.env` 不進 |
| `.env.secrets.example` → `.env.secrets`（專案根目錄） | 有交易/簽章能力的密鑰（`BINANCE_API_KEY`/`SHIOAJI_*`）；本機/VM 各自維護，**永遠不同步** | `.env.secrets.example` 進，`.env.secrets` 不進 |
| `librae/config/markets.yaml` | 市場成本 + 保證金參數 | yes |
| `librae/config/symbols.yaml` | symbol → market/data_source 對應 | yes |
| `strategies/*/config.yaml` | 策略參數 + 通知 | yes |
| `strategies/experiments/<name>/config.yaml` | 實驗參數（只有走 `run.py`/`RunConfig` 模式的實驗才有；大部分實驗是獨立的 `factor_research.py` 腳本，不用這套設定） | yes |
| `deploy/timescale_init.sql` | DB schema | yes |

---

## 相關文件

- [`librae/README.md`](librae/README.md) — 引擎架構、API、類型系統
- [`architecture.md`](architecture.md) — 系統分層、命名慣例、VM 部署與策略管理
- [`docs/decisions/`](docs/decisions/) — 架構決策記錄
- [`docs/plans/`](docs/plans/) — 執行計劃
- [`docs/learnings/ERRORS.md`](docs/learnings/ERRORS.md) — 除錯記錄（症狀/根因/修法/預防）
