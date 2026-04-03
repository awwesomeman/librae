# GCE 部署指南（TimescaleDB + Sim）

> 與 `windows_server_deploy.md` 對應，改在 Google Compute Engine Linux VM 上部署。
> 差異：Docker 原生運行、IAP 取代 VPN、無 WSL2 相關坑。

---

## 架構

```
┌─────────────────────────────────────────┐
│  GCE VM (e2-small, Ubuntu 22.04)       │
│                                         │
│  Docker                                 │
│  ├── quant_timescaledb  :5432           │
│  └── sim / signal monitor processes    │
└──────────────┬──────────────────────────┘
               │ IAP TCP Tunnel / TIMESCALE_DSN
┌──────────────┴──────────────────────────┐
│  開發機（Mac / Linux）                   │
│  ├── 回測 / 訊號研究 → 寫入遠端 DB      │
│  └── Grafana :3000 → 查詢遠端 DB        │
└─────────────────────────────────────────┘
```

> Grafana 建議在本機開啟（幾乎不吃資源），直接連 VPS 的 TimescaleDB。

---

## Part 1: GCE VM 端

### 1.1 建立 VM

```bash
gcloud compute instances create quant-server \
  --zone=asia-east1-b \
  --machine-type=e2-small \
  --boot-disk-size=30GB \
  --boot-disk-type=pd-ssd \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud
```

### 1.2 安裝 Docker

```bash
gcloud compute ssh quant-server --tunnel-through-iap

sudo apt update && sudo apt install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER && newgrp docker
```

### 1.3 Clone & 啟動

```bash
git clone git@github-quant-strategy:awwesomeman/quant-strategy-lab.git
cd quant-strategy-lab/deploy
```

建立 `deploy/.env`（不要 commit）：

```bash
POSTGRES_PASSWORD=<strong_password>
GF_SECURITY_ADMIN_PASSWORD=<strong_password>
```

```bash
docker compose up -d timescaledb grafana
```

### 1.4 初始化 Schema

```bash
# 等約 10 秒讓 TimescaleDB 就緒
docker exec -i quant_timescaledb psql -U quant -d quant < timescale_init.sql
```

### 1.5 設定 Grafana

```bash
cd ..
python3 scripts/setup_grafana.py --grafana-password <密碼>
```

---

## Part 2: 開發機端

### 2.1 建立 IAP Tunnel

```bash
gcloud compute ssh quant-server --tunnel-through-iap \
  -- -L 5432:localhost:5432 -L 3000:localhost:3000
```

> Tunnel 開著期間，本機 `localhost:5432` 直通 VM 的 TimescaleDB。

### 2.2 環境設定 & 驗證

```bash
export TIMESCALE_DSN="postgresql://quant:<密碼>@localhost:5432/quant"

# 驗證
python -c "
import psycopg2, os
conn = psycopg2.connect(os.environ['TIMESCALE_DSN'])
cur = conn.cursor()
cur.execute('SELECT count(*) FROM backtest_runs')
print(f'backtest_runs: {cur.fetchone()[0]} rows')
conn.close()
"
```

### 2.3 跑回測 / Streamlit / Grafana

```bash
python scripts/run_backtest.py --months 6 --sample oos
python -m streamlit run app/streamlit_performance.py --server.port 8502
# Grafana → http://localhost:3000
```

---

## 與 Windows Server 部署的差異

| 項目 | Windows Server | GCE |
|------|---------------|-----|
| Docker | Docker Desktop + WSL2 | `apt install docker.io`，原生 |
| 網路存取 | 自架 WireGuard VPN | IAP TCP Tunnel（零配置） |
| 防火牆 | PowerShell `New-NetFirewallRule` | GCP VPC Firewall（預設 deny all） |
| docker-compose ports | 改 `0.0.0.0:5432` | 維持 `127.0.0.1:5432` 即可 |
| `docker exec` hang | WSL2 已知問題 | 不會發生 |

---

## 安全注意事項

- docker-compose.yml 維持 `127.0.0.1` bind，透過 IAP tunnel 存取，**不要開 0.0.0.0**
- GCE 預設 firewall deny all inbound，不需額外設規則
- 密碼用 `deploy/.env` 管理，加入 `.gitignore`
- 建議開啟 [OS Login](https://cloud.google.com/compute/docs/instances/managing-instance-access) 管理 SSH 存取
