# 2026-03-30 — TimescaleDB port binding 改為可配置

> 狀態：implemented
> 注記：TSDB_BIND 環境變數已在 docker-compose.yml 落地

## 背景

`docker-compose.yml` 將 TimescaleDB port 硬編碼為 `100.123.243.93:5432`（Tailscale VPN IP）。當 Tailscale 未安裝或未執行時，該 IP 不存在於主機的任何網路介面，導致容器無法啟動。

VPS 部署時遇到此問題：Tailscale container 尚未就緒或未安裝，TimescaleDB 即嘗試 bind 到不存在的 IP。

## 決策

使用環境變數 `TSDB_BIND` 控制 TimescaleDB 的 bind address，預設 `127.0.0.1`。

```yaml
# deploy/docker-compose.yml
ports:
  - "${TSDB_BIND:-127.0.0.1}:5432:5432"
```

## 使用方式

| 環境 | `.env` 設定 | 效果 |
|------|-------------|------|
| VPS（有 Tailscale） | `TSDB_BIND=100.123.243.93` | 僅 tailnet 可連 |
| 本地開發 | 不設（預設 `127.0.0.1`） | 僅本機可連 |
| 測試 / CI | `TSDB_BIND=0.0.0.0` | 所有介面可連 |

## 替代方案（未採用）

- **`depends_on` + healthcheck**：讓 TimescaleDB 等 Tailscale 就緒後再啟動。但 Tailscale container 用 `network_mode: host`，與 `quant_network` bridge 不在同一 network，`depends_on` 無法保證 tailscale0 介面已建立。
- **改用 `0.0.0.0`**：所有介面都暴露，安全性較差。
