
## 2026-03-27 Grafana datasource uid 衝突

**問題**：provisioning YAML 設 `uid: timescaledb`，但 Grafana volume 已有 plugin（grafana-postgresql-datasource）自動產生的 uid `P40AE60E18F02DE32`，導致 provisioning 失敗 crash。

**根因**：Grafana volume 沒有清除就重建 container，舊 plugin 狀態殘留。

**修法**：provisioning YAML 的 uid 要和 Grafana 實際使用的 uid 一致；或清除 volume 讓 Grafana 重新初始化。

**預防**：新環境部署先跑 `docker volume rm grafana_data` 再啟動，確保乾淨狀態。
