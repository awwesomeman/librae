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
# 自動偵測 datasource uid + 產生三板 JSON + 部署（首次必跑）
python scripts/setup_grafana.py

# 或手動：只重新產生 JSON（不部署）
.venv/bin/python grafana/generate_dashboards.py
```

---

## 換環境注意事項

### Grafana datasource uid（最常見問題）

每個 Grafana 實例會自動產生不同的 datasource uid，導致儀表板空白。
**首次部署或 Grafana volume 重建後必跑：**

```bash
python scripts/setup_grafana.py
```

此腳本會自動偵測 uid、更新 generator、重新部署三板。支援自訂參數：

```bash
python scripts/setup_grafana.py --grafana-url http://host:3000 --grafana-user admin --grafana-password secret
```

### TimescaleDB 密碼

預設密碼 `quant_secret`（開發用）。Production 環境請修改：
1. `deploy/docker-compose.yml` → `POSTGRES_PASSWORD`
2. `grafana/provisioning/datasources/timescaledb.yaml` → `secureJsonData.password`
3. 環境變數 `TIMESCALE_DSN` 同步更新

### Python 版本

需要 Python >= 3.10。建議 3.11。

---

## 環境變數

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
