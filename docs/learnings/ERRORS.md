
## 2026-03-27 Grafana datasource uid 衝突

**問題**：provisioning YAML 設 `uid: timescaledb`，但 Grafana volume 已有 plugin（grafana-postgresql-datasource）自動產生的 uid `P40AE60E18F02DE32`，導致 provisioning 失敗 crash。

**根因**：Grafana volume 沒有清除就重建 container，舊 plugin 狀態殘留。

**修法**：provisioning YAML 的 uid 要和 Grafana 實際使用的 uid 一致；或清除 volume 讓 Grafana 重新初始化。

**預防**：新環境部署先跑 `docker volume rm grafana_data` 再啟動，確保乾淨狀態。

## 2026-03-27 Session 規範違反（系統性問題）

**問題清單：**
1. Session 啟動未執行必讀清單（SOUL.md, USER.md, AGENTS.md, memory）
2. Git push 多次未詢問 Jason（違反 AGENTS.md Git Push 規則）
3. Sub-agent prompt 多次貼大段背景（違反 Token 控制規範）
4. /simplify 未追蹤觸發條件，需補做
5. agent_runs.jsonl 大量 sprint 未寫入
6. tools 使用未標在回報 META 行
7. 沒有在里程碑完成後主動給彙整回報

**根因：**
- Session 啟動沒有確實執行必讀清單
- 工作忙碌時傾向省略流程步驟

**預防：**
- 每次 session 開始讀 SETUP.md（必讀清單在裡面）
- Context 超過 60% 主動提醒 /new
- Sprint 完成後立即寫 agent_runs.jsonl，不累積到最後

## 2026-03-28 Grafana 12 dashboard variable 不顯示

**症狀**：Backtest dashboard 的 `run_id` 下拉顯示紅色三角形警告，無法選取任何值。Grafana API proxy query 能正常回傳資料，但 dashboard 前端 variable 查詢失敗。

**根因（三層問題疊加）**：

1. **Provisioning datasource 缺少 `jsonData.database`**：Grafana 12 的 `grafana-postgresql-datasource` 除了 top-level `database` 欄位外，還需要在 `jsonData` 裡也設定 `database`，否則 variable query 報「no default database configured」。
2. **Provisioning 掛載路徑不一致**：專案目錄重組後 provisioning 檔從 `grafana/provisioning` 移到了 `app/grafana/provisioning`，docker-compose.yml 已更新但舊 container 用 `docker-compose restart` 不會重新套用 volume 設定，需要 `docker-compose up -d` 重建 container。
3. **`editable: false` 阻擋修復**：provisioning 設定的 datasource 預設 `readOnly`，無法透過 API 或 UI 修改，必須改 provisioning YAML + 重建 container。

**修法**：
```yaml
# app/grafana/provisioning/datasources/timescaledb.yaml
jsonData:
  database: quant        # <-- Grafana 12 必須
  sslmode: disable
  timescaledb: true
editable: true            # <-- 允許 runtime 修改
```

**同時修正 dashboard generator**：
- variable 加 `definition` 和 `rawQuery: true`（Grafana 12 需要）
- 每個 target 加 `datasource` 欄位（Grafana 12 per-target datasource）

**預防**：
- Grafana 升版後檢查 provisioning datasource 的 `jsonData` 欄位需求
- 目錄重組後用 `docker-compose up -d`（不是 `restart`）確保 volume 掛載更新
- Provisioning datasource 用 `editable: true` 方便除錯
