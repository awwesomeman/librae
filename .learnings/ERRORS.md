
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
