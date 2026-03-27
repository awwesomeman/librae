# quant-strategy-lab

量化策略研究與監控平台。TrendPullback 策略 + 自建回測引擎 + TimescaleDB + Grafana 三板儀表板。

---

## 快速開始

### 環境需求

- Python 3.11
- Docker + Docker Compose
- Git

### 1. Clone & 安裝

```bash
git clone git@github-quant-strategy:awwesomeman/quant-strategy-lab.git
cd quant-strategy-lab

python3.11 -m venv .venv
.venv/bin/pip install -e .
```

### 2. 啟動 TimescaleDB + Grafana

```bash
cd deploy
docker compose up -d timescaledb grafana

# 等 TimescaleDB 啟動後初始化 schema（首次必做）
sleep 10
docker exec -i quant_timescaledb psql -U quant -d quant < timescale_init.sql
```

### 3. 跑一次回測（填充資料）

```bash
cd ..
.venv/bin/python scripts/run_backtest_lumibot_btc.py --months 6 --sample oos
```

### 4. 啟動 Streamlit

```bash
.venv/bin/python -m streamlit run app/streamlit_performance.py --server.port 8502
```

### 5. 部署 Grafana 儀表板

```bash
# 重新產生三板 JSON 並部署（首次或改版後）
.venv/bin/python grafana/generate_dashboards.py

python3 -c "
import json, requests
for f in ['backtest_dashboard.json', 'sim_dashboard.json', 'live_dashboard.json']:
    d = json.load(open(f'grafana/dashboards/{f}'))
    d.pop('id', None)
    r = requests.post('http://localhost:3000/api/dashboards/db',
        json={'dashboard': d, 'folderId': 0, 'overwrite': True},
        auth=('admin', 'admin'))
    print(f, r.json().get('status'))
"
```

---

## 注意事項

### Grafana datasource uid

Grafana 啟動時會自動產生 datasource uid。若儀表板顯示空白，需要更新 `grafana/generate_dashboards.py` 的 `DATASOURCE` uid：

```bash
# 查詢實際 uid
curl -s http://admin:admin@localhost:3000/api/datasources | python3 -c "
import json,sys
for d in json.load(sys.stdin):
    print(d['name'], d['uid'])
"
```

再把 `generate_dashboards.py` 第一行的 `DATASOURCE` uid 改成查詢結果，重新跑步驟 5。

### 環境變數

| 變數 | 預設值 | 說明 |
|------|--------|------|
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
│   ├── run_backtest_lumibot_btc.py  # 主回測腳本
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
| `python scripts/run_backtest_lumibot_btc.py --months 6` | 跑回測 |
| `python scripts/run_backtest_lumibot_btc.py --dry-run` | 回測不寫 DB |
| `python scripts/run_monitor_once.py --dry-run` | 單次訊號 dry-run |
| `python grafana/generate_dashboards.py` | 重新產生三板 JSON |

---

## 相關文件

- `docs/implementation_plan.md`：開發計劃與進度
- `decisions/`：重大架構決策記錄
- `config/markets.yaml`：市場與標的設定
- `.learnings/ERRORS.md`：已知踩坑記錄
