
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

## 2026-03-28 Grafana provisioned dashboard 更新後不生效

**症狀**：推版後 `docker-compose up -d` 重啟 Grafana，但 dashboard 仍顯示舊版（舊欄位、舊 SQL、舊時間篩選）。

**根因**：`generate_dashboards.py` 輸出到 `app/grafana/dashboards/`，但 Grafana provisioning config（`default.yaml`）讀的是 `/etc/grafana/provisioning/dashboards/json/`。docker-compose 只掛載了 `app/grafana/provisioning/` → `/etc/grafana/provisioning/`，所以 `dashboards/` 目錄從未被 Grafana 讀取。

**修法**：把 `generate_dashboards.py` 的輸出路徑改為 `app/grafana/provisioning/dashboards/json/`，跟 provisioning config 的 `path` 對齊。

**預防**：
- Dashboard JSON 輸出路徑必須在 provisioning 掛載路徑內
- 部署後檢查 Grafana container 內是否看得到檔案：`docker exec quant_grafana ls /etc/grafana/provisioning/dashboards/json/`
- Provisioning 的 `updateIntervalSeconds: 30` 會自動掃描，但首次需要 restart 才載入

## 2026-03-29 Trade Detail 時間篩選與 Trade Signals 不一致

**症狀**：Trade Signals chart 顯示的 entry/exit dots 與 Trade Detail table 顯示的交易筆數對不上。

**根因**：Trade Detail 用 `$__timeFilter(entry_ts)` 只依 entry_ts 做雙向篩選。當交易的 exit 在時間視窗內但 entry 在視窗外時，chart 有 exit dot 但 table 沒有該筆交易。

**修法**：改為 `$__timeFilter(entry_ts) OR $__timeFilter(exit_ts)`，只要 entry 或 exit 任一在視窗內就顯示，與 Trade Signals 的獨立 dot 篩選邏輯對齊。

**預防**：多 panel 共用同一資料集時，確保篩選條件語意一致。

## 2026-03-29 Grafana timeseries tooltip 顯示游標位置而非資料點時間

**症狀**：Trade Signals chart tooltip 時間與 Trade Detail table 的 Entry Time 不一致，且差距不固定（排除時區問題）。

**根因**：Grafana timeseries panel 在 `tooltip: "multi"` mode 下，tooltip 時間對齊最密集的 series（OHLCV 每小時一根 bar），不是 scatter point 的實際時間。密集的 OHLCV 線也搶走 scatter dot 的 hover 事件。

**修法**：拆成兩個 panel — OHLCV Price（線圖）和 Entry/Exit Signals（散點圖），tooltip 改 `"single"` mode，開啟 `graphTooltip: 1`（shared crosshair）同步游標。

**教訓**：在追問題前先用 DB 查詢驗證資料是否一致，避免在顯示層問題上浪費時間修資料層。本次 DB 驗證確認 `strategy_signals.ts` 與 `trade_blotter.entry_ts/exit_ts` diff = 0，問題純粹在 Grafana UI 行為。

## 2026-03-29 Short 交易 PnL 方向錯誤 + 前後端指標計算不一致

**症狀**：儀表板顯示 Short 部位價格走跌但報酬率為負。

**根因（兩層問題）**：
1. **後端 `calc_pnl`**：公式 `(exit - entry) * qty`，qty 永遠正數，Short 時 exit < entry 得到負值但應為正（做空獲利）。
2. **前後端計算不一致**：`gross_pnl` 存 DB 但 Grafana/Streamlit 各自用不同公式動態重算 return %，且都沒考慮 side。

**修法**：
- 後端：`_PositionState` 加 `direction` property（short=-1, long=+1），所有 PnL 計算乘以 direction
- 新增 `gross_return`/`net_return` 欄位，後端算好存入 `trade_blotter`
- 前端統一改為 `SELECT gross_return, net_return`，不再動態計算

**預防**：
- 指標計算應集中在後端（single source of truth），前端只做呈現
- 涉及方向性的計算（PnL、return）必須考慮 side/direction
