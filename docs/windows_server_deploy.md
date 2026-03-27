# Windows Server 部署指南（TimescaleDB + Grafana 集中式）

> 目標：在一台 Windows 機器上跑 TimescaleDB + Grafana，透過 VPN 對外開放。
> 其他開發機只需設定環境變數連線，不再各自跑 Docker。

---

## 架構

```
┌─────────────────────────────────────────────┐
│  Windows Server（VPN 內網 IP，例如 10.8.0.1） │
│                                             │
│  Docker Desktop                             │
│  ├── quant_timescaledb  :5432               │
│  └── quant_grafana      :3000               │
└──────────────┬──────────────────────────────┘
               │ VPN tunnel
┌──────────────┴──────────────────────────────┐
│  開發機（Mac / Linux / 其他 Windows）         │
│  只跑 Python：回測、Streamlit                │
│  TIMESCALE_DSN=postgresql://quant:xxx@10.8.0.1:5432/quant │
└─────────────────────────────────────────────┘
```

---

## Part 1: Windows Server 端

### 1.1 安裝 Docker Desktop

1. 下載 [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/)
2. 安裝時啟用 **WSL 2 backend**（建議）
3. 安裝完成後重開機，確認 `docker info` 有回應

### 1.2 Clone repo & 啟動服務

```powershell
git clone git@github-quant-strategy:awwesomeman/quant-strategy-lab.git
cd quant-strategy-lab
```

**啟動前必改 `deploy/docker-compose.yml`：**

```yaml
# 1) TimescaleDB — 改 bind 為 0.0.0.0，讓 VPN client 可連
ports:
  - "0.0.0.0:5432:5432"   # 原本是 127.0.0.1:5432:5432

# 2) 改 production 密碼
environment:
  - POSTGRES_PASSWORD=<你的強密碼>

# 3) Grafana — 設定管理員密碼
environment:
  - GF_SECURITY_ADMIN_PASSWORD=<你的強密碼>
```

同步更新 `grafana/provisioning/datasources/timescaledb.yaml`：

```yaml
secureJsonData:
  password: <同上面的 POSTGRES_PASSWORD>
```

**啟動：**

```powershell
cd deploy
docker compose up -d timescaledb grafana
```

### 1.3 初始化 Schema

```powershell
# 等 TimescaleDB 啟動（約 10 秒）
docker exec -i quant_timescaledb psql -U quant -d quant < timescale_init.sql
```

> **踩坑：`docker exec` 在某些環境會 hang**
>
> 如果 `docker exec` 無回應，改用 host 端直接連：
> ```powershell
> # 先裝 psql，或用 Python
> python -c "
> import psycopg2
> conn = psycopg2.connect('postgresql://quant:<密碼>@localhost:5432/quant')
> conn.autocommit = True
> conn.cursor().execute(open('timescale_init.sql').read())
> print('Schema OK')
> conn.close()
> "
> ```

### 1.4 設定 Grafana 儀表板

```powershell
cd ..
python scripts/setup_grafana.py --grafana-password <你設的密碼>
```

> **踩坑：Grafana datasource uid 不匹配**
>
> 每個 Grafana 實例的 datasource uid 不同。`setup_grafana.py` 會自動偵測並更新。
> 如果儀表板空白，重跑這個腳本即可。
> 如果 Grafana volume 重建過，要先清除再啟動：
> ```powershell
> docker compose down
> docker volume rm deploy_grafana_data
> docker compose up -d
> # 等 10 秒後重跑
> python scripts/setup_grafana.py --grafana-password <密碼>
> ```

### 1.5 Windows 防火牆

開放 VPN 子網對以下 port 的存取：

```powershell
# PowerShell (Admin)
New-NetFirewallRule -DisplayName "TimescaleDB" -Direction Inbound -LocalPort 5432 -Protocol TCP -Action Allow -RemoteAddress 10.8.0.0/24
New-NetFirewallRule -DisplayName "Grafana" -Direction Inbound -LocalPort 3000 -Protocol TCP -Action Allow -RemoteAddress 10.8.0.0/24
```

> 把 `10.8.0.0/24` 換成你實際的 VPN 子網。**不要開放到 0.0.0.0。**

### 1.6 VPN 設定（WireGuard 範例）

在 Windows Server 安裝 [WireGuard](https://www.wireguard.com/install/)，建立 server config：

```ini
# /etc/wireguard/wg0.conf (或 WireGuard GUI 設定)
[Interface]
Address = 10.8.0.1/24
ListenPort = 51820
PrivateKey = <server_private_key>

[Peer]   # 開發機 Mac
PublicKey = <client_public_key>
AllowedIPs = 10.8.0.2/32
```

Client 端（Mac）：

```ini
[Interface]
Address = 10.8.0.2/24
PrivateKey = <client_private_key>
DNS = 1.1.1.1

[Peer]
PublicKey = <server_public_key>
Endpoint = <server_public_ip>:51820
AllowedIPs = 10.8.0.0/24
PersistentKeepalive = 25
```

---

## Part 2: 開發機端（Mac / Linux）

開發機**不需要**跑 Docker，只需 Python 環境。

### 2.1 安裝

```bash
git clone git@github-quant-strategy:awwesomeman/quant-strategy-lab.git
cd quant-strategy-lab
python3.11 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install streamlit   # pyproject.toml 未包含，需手動裝
```

> **踩坑：`streamlit` 不在 `pyproject.toml` dependencies 裡**
>
> 回測和 DB 寫入不需要它，但 Step 4 的 Streamlit UI 需要。

### 2.2 設定環境變數

```bash
# .env 或 shell profile
export TIMESCALE_DSN="postgresql://quant:<密碼>@10.8.0.1:5432/quant"
```

### 2.3 驗證連線

```bash
.venv/bin/python -c "
import psycopg2, os
conn = psycopg2.connect(os.environ['TIMESCALE_DSN'])
cur = conn.cursor()
cur.execute('SELECT count(*) FROM backtest_runs')
print(f'backtest_runs: {cur.fetchone()[0]} rows')
conn.close()
"
```

### 2.4 跑回測（資料寫到 Server 的 DB）

```bash
.venv/bin/python scripts/run_backtest_lumibot_btc.py --months 6 --sample oos
```

### 2.5 開 Streamlit

```bash
.venv/bin/python -m streamlit run app/streamlit_performance.py --server.port 8502
```

### 2.6 看 Grafana

瀏覽器開 `http://10.8.0.1:3000`，帳號 admin / 你設的密碼。

---

## 踩坑總覽

| 問題 | 症狀 | 解法 |
|------|------|------|
| `schemas/canonical_schema.json` 被誤刪 | `import quant_lab` 直接 crash：`RuntimeError: Canonical schema not found` | `git show 9a3d1c6^:schemas/canonical_schema.json > schemas/canonical_schema.json` 恢復 |
| `docker exec` hang | 命令不回傳，timeout | 改用 host 端 `psycopg2` 直接連 DB 執行 SQL |
| Grafana 儀表板空白 | datasource uid 不匹配 | 跑 `python scripts/setup_grafana.py`，會自動偵測並修正 |
| Grafana volume 殘留舊 uid | provisioning 衝突，Grafana crash | `docker volume rm deploy_grafana_data` 後重啟 |
| `streamlit` 未安裝 | `No module named streamlit` | `pip install streamlit`（未列在 pyproject.toml） |
| TimescaleDB 只 bind 127.0.0.1 | 遠端機連不上 5432 | docker-compose.yml 改為 `0.0.0.0:5432:5432` |
| Docker daemon 未就緒 | `docker info` 無回應，所有 docker 命令 hang | 等 Docker Desktop / OrbStack 完全啟動 |

---

## 安全注意事項

- TimescaleDB 和 Grafana **僅透過 VPN 暴露**，不要直接開到公網
- 務必修改預設密碼 `quant_secret` 和 Grafana `admin`
- docker-compose.yml 中的密碼不要 commit 到 repo，用 `.env` 檔管理：
  ```bash
  # deploy/.env
  POSTGRES_PASSWORD=<strong_password>
  GF_SECURITY_ADMIN_PASSWORD=<strong_password>
  ```
