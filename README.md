# quant-strategy-lab

量化策略研究與監控平台。TrendPullback 策略 + 自建回測引擎 + TimescaleDB + Grafana 三板儀表板。

---

## 使用情境

### A. 從其他裝置連進已部署的伺服器

1. 安裝 [Tailscale](https://tailscale.com/download) 並登入同一帳號
2. 透過 Tailscale 虛擬 IP 存取：
   - Grafana：`http://<tailscale-ip>:3000`（預設帳密 admin / admin）
   - TimescaleDB：`<tailscale-ip>:5432`（user: `quant` / password: `quant_secret`）

### B. 首次部署（GCE / 本地）

詳細部署步驟請參考：
- GCE：[docs/gce_deploy.md](docs/gce_deploy.md)
- Windows：[docs/windows_server_deploy.md](docs/windows_server_deploy.md)

簡要流程：

```bash
# 1. Clone & 安裝
git clone git@github-quant-strategy:awwesomeman/quant-strategy-lab.git
cd quant-strategy-lab
python3.11 -m venv .venv && .venv/bin/pip install -e .

# 2. 設定環境變數
cd deploy
cp .env.example .env   # 填入 TS_AUTHKEY（VPN 用，可選）

# 3. 啟動服務（不需要 VPN 可省略 tailscale）
docker compose up -d

# 4. 初始化 DB schema（首次必做）
sleep 10
docker exec -i quant_timescaledb psql -U quant -d quant < timescale_init.sql

# 5. 部署 Grafana 儀表板（首次必做）
cd .. && python scripts/setup_grafana.py
```

### C. 本地開發（不需要 VPN）

只啟動 DB 和 Grafana：

```bash
cd deploy
docker compose up -d timescaledb grafana
```

---

## 環境變數

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `TS_AUTHKEY` | 必填 | Tailscale Auth Key（從 Admin Console 產生） |
| `GF_SECURITY_ADMIN_PASSWORD` | `admin` | Grafana admin 密碼 |
| `TIMESCALE_DSN` | `postgresql://quant:quant_secret@localhost:5432/quant` | TimescaleDB 連線 |
| `CCXT_API_KEY` | （選填）| Binance API key，未設定為 read-only |
| `CCXT_API_SECRET` | （選填）| Binance API secret |
| `MONITOR_SYMBOL` | `BTC/USDT` | Scheduler 監控標的 |
| `MONITOR_TIMEFRAME` | `1h` | Scheduler 時間框架 |

### 啟動訊號排程（Monitor 板）

```bash
bash scripts/monitor/run_scheduler.sh
# 每小時整點執行一次，結果寫入 TimescaleDB（mode=sim）
```

---

## 目錄結構

```
quant-strategy-lab/
├── app/                    # Streamlit 策略研究工具
├── config/
│   └── markets.yaml        # 市場/標的 config（兩層架構）
├── deploy/
│   ├── docker-compose.yml
│   ├── .env                # 本地環境變數（不入 git）
│   └── timescale_init.sql  # TimescaleDB schema（首次必跑）
├── docs/
│   └── implementation_plan.md
├── grafana/
│   ├── dashboards/         # 三板 JSON（由 generate_dashboards.py 產生）
│   ├── generate_dashboards.py  # 修改儀表板的唯一入口
│   └── provisioning/
├── quant_lab/
│   ├── db/                 # TimescaleDB 讀寫層
│   ├── signal_engine/      # TrendPullback pure function
│   ├── monitoring/         # SignalResult, run_monitor
│   └── adapters/           # CryptoAdapter, MarketHub
├── scripts/
│   ├── run_backtest.py  # 主回測腳本
│   ├── run_monitor_once.py
│   └── monitor/
│       ├── scheduler.py
│       └── run_scheduler.sh
└── tests/                  # pytest，405/405
```

---

## 主要指令

| 指令 | 說明 |
|------|------|
| `pytest tests/ -v` | 跑所有測試 |
| `python scripts/run_backtest.py --months 6` | 跑回測 |
| `python scripts/run_backtest.py --dry-run` | 回測不寫 DB |
| `python scripts/run_monitor_once.py --dry-run` | 單次訊號 dry-run |
| `python grafana/generate_dashboards.py` | 重新產生三板 JSON |

---

## 相關文件

- `docs/implementation_plan.md`：開發計劃與進度
- `decisions/`：重大架構決策記錄
- `config/markets.yaml`：市場與標的設定
- `.learnings/ERRORS.md`：已知踩坑記錄
