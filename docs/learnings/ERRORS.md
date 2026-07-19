
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

## 2026-07-19 GCP VM 手動貼 SSH 公鑰沒生效，改用 gcloud CLI 寫入 metadata

**症狀**：照 GCP Console「編輯 VM → 安全殼層金鑰 → 新增項目」貼公鑰、儲存後，`ssh <user>@<vm-ip>` 仍然 `Permission denied (publickey)`。改用 `gcloud compute ssh` 卻能正常連線。

**根因**：查 VM instance metadata（`gcloud compute instances describe <instance> --zone=<zone> --format="value(metadata.items)"`）發現手動貼上去的公鑰**根本沒有出現在 metadata 裡**——Console 那次儲存沒有真的存進去（原因不明）。不是 OS Login 把 metadata key 忽略掉（另外查過 `enable-oslogin` 在專案和實例層級都沒被設定）。`gcloud compute ssh` 能連是因為它用自己一套獨立機制（`~/.ssh/google_compute_engine` + 自動寫入的短效 `google-ssh` metadata key），跟手動貼的那把公鑰完全無關，容易誤以為「金鑰有生效」。

**修法**：改用指令直接寫入 metadata，不透過網頁手動貼：
```bash
# 先讀出既有 ssh-keys，跟新公鑰合併成一個檔案（add-metadata 是整個值覆蓋，不是 append）
gcloud compute instances add-metadata <instance> --zone=<zone> \
  --metadata-from-file ssh-keys=<合併後的檔案路徑>
```
寫入後等 20-30 秒讓 VM 上的開機代理程式同步，再測 `ssh <user>@<vm-ip>`。

**預防**：
- Console 手動貼 SSH 公鑰後，用 `gcloud compute instances describe <instance> --format="value(metadata.items)"` 實際查一次確認金鑰真的寫進去了，不要只看畫面上「已儲存」就假設有效。
- 有 `gcloud` CLI 可用時，優先用 `add-metadata` 寫入，比網頁手動編輯可靠，也方便之後腳本化重現。

## 2026-07-19 ccxt Binance sandbox 指向已棄用的 testnet.binance.vision

**症狀**：`CryptoAdapter(sandbox=True)` 呼叫 Binance demo API 時得到 `ccxt.base.errors.AuthenticationError: binance {"code":-2015,...}`，一開始誤判成 API key 沒設 IP 白名單。

**根因**：ccxt 4.5.66 的 `set_sandbox_mode(True)` 對 Binance spot 仍然指向 Binance 已棄用的 `testnet.binance.vision`；Binance 現行的 demo 環境網域是 `demo-api.binance.com`（上游 issue：ccxt/ccxt#27266，2026-07 仍開著）。

**修法**：`brokers/crypto_adapter.py` 加 `_patch_binance_sandbox_urls()`，在 `set_sandbox_mode(True)` 之後手動把 `exchange.urls['api']` 裡 `testnet.binance.vision` 字串取代成 `demo-api.binance.com`，只對 `exchange_id == "binance"` 生效。

**預防**：
- 遇到 sandbox/demo 環境的認證錯誤，先確認實際打到的網域對不對（`exchange.urls`），不要直接假設是金鑰或白名單問題。
- ccxt 對特定交易所的 sandbox URL 可能落後交易所自己的遷移，升級 ccxt 版本後應該重新檢查這個 patch 還需不需要。

## 2026-07-19 Shioaji login() 不再接受 person_id

**症狀**：`ShioajiAdapter.__init__` 呼叫 `self._api.login(api_key=..., secret_key=..., person_id=...)` 得到 `TypeError: Shioaji.login() got an unexpected keyword argument 'person_id'`（已安裝 shioaji 1.3.3）。

**根因**：Shioaji SDK 上游把 `login()` 的簽名改了——`person_id` 不再是 `login()` 的參數，改成從 `api_key` 對應的帳號自動決定；`person_id` 現在只有 `activate_ca()`（選擇要用哪個帳號的憑證）才需要。

**修法**：`login()` 呼叫拿掉 `person_id`，只在後面 `activate_ca(ca_path=..., ca_passwd=..., person_id=...)` 才傳。

**預防**：Shioaji SDK 版本升級後，函式簽名可能跟著變，遇到 `TypeError: unexpected keyword argument` 先查該版本的實際簽名，不要假設官方文件範例一定跟裝到的版本一致。

## 2026-07-19 psycopg2 SimpleConnectionPool 不驗證連線，壞掉的連線會一直被重複發放

**症狀**：本機長時間（約 1 小時）跑背景 `LiveTrader` 測試，中途 Tailscale 斷線又重連後，`DB update_heartbeat failed: connection already closed` 開始每次心跳都報錯，直到手動重啟程式才恢復。

**根因**：`psycopg2.pool.SimpleConnectionPool` 在 `getconn()` 時不會驗證連線是否還活著——網路短暫中斷把某條連線弄壞後，pool 仍然把這條壞掉的連線繼續發放出去，之後每次用到就失敗，直到程式重啟、pool 被重建。

**修法**：`db/__init__.py` 的 `get_conn()` 改成 `pool.putconn(conn, close=bool(conn.closed))`——歸還連線時如果 `conn.closed` 是真的，直接丟棄而不是放回 pool 重複使用。

**預防**：用 `SimpleConnectionPool` 時，任何長時間跑的程序都要在歸還連線前檢查 `conn.closed`，不能假設 pool 自己會處理壞連線；跟資料庫之間有不穩定網路（VPN/Tailscale）時尤其容易踩到。

## 2026-07-19 Shioaji API key 只有模擬權限，正式登入報「Token doesn't have production permission」

**症狀**：幫 `get_ohlcv(..., data_source="shioaji")` 寫歷史資料 fetcher，預設用 `ShioajiAdapter()`（`simulation=False`）登入，得到 `Exception: {'status': {'status_code': 400}, ... 'detail': "Token doesn't have production permission."}`。

**根因**：本機這把 `SHIOAJI_API_KEY` 是唯讀、只申請了模擬（simulation）環境的登入權限，沒有開正式環境登入權限——這跟「有沒有 CA、能不能下單」是兩件事，login 本身的模擬/正式權限是另一層限制。

**修法**：歷史資料 fetcher（`strategies/data/ohlcv.py` 的 `_shioaji_fetcher`）固定用 `ShioajiAdapter(simulation=True)` 登入——回測用歷史資料本來就不需要正式權限，也讓只有模擬權限的 key 能用。

**預防**：Shioaji 的「模擬 vs 正式」是登入層級的權限，不是下單層級（下單層級是有沒有 CA），申請/使用 key 時兩層要分開確認；純資料用途沒有理由要求正式登入權限。

## 2026-07-19 shioaji 1.7.0 的下單 enum 從 shioaji.order.* 搬到 shioaji.*

**症狀**：VM 上用正式 full 權限 key + CA 對台指期實際掛一筆限價單，`ShioajiAdapter.place_order()` 內部呼叫 `sj.order.Action.Buy` 時噴 `AttributeError: module 'shioaji.order' has no attribute 'Action'`。本機開發時用的是 shioaji 1.3.3，這段程式碼從沒被跑到過（既有測試只覆蓋 read-only guard 那條 raise 路徑，沒有真的呼叫過 `place_order()` 內部邏輯）。

**根因**：`deploy/Dockerfile` 裝的是 shioaji 1.7.0（`pip install '.[tw-live]'` 沒釘死版本，抓到的是當下最新版）。1.4 之後這些下單用的 enum（`Action`、`FuturesPriceType`、`StockPriceType`、`OrderType`）從 `shioaji.order.*` 搬到頂層 `shioaji.*`，member 名稱沒變（`Buy`/`Sell`/`LMT`/`MKT`/`ROD` 都還在），只是路徑變了。

**修法**：`brokers/shioaji_adapter.py` 的 `place_order()` 全部改成 `sj.Action`/`sj.FuturesPriceType`/`sj.StockPriceType`/`sj.OrderType`（拿掉 `.order.` 這一段）。同時補了 `TestPlaceOrder`（mock `shioaji` 模組），實際測試 `place_order()` 組出來的 `Order()` 呼叫參數，不再只測 guard 那條路徑。

**預防**：
- 下單這條路徑風險高、卻是最容易「本機測過就以為沒事」的地方——本機用的 SDK 版本、Docker image 裡實際裝的 SDK 版本可能不一致，光靠本機跑過不代表 VM 上真的能跑。
- 任何呼叫第三方 SDK enum/常數的程式碼，都要有測試真的呼叫到那一行，不能只測外層的 guard/例外路徑；「有測試」不等於「測到了會出錯的地方」。
- 順帶一提：這次也看到 shioaji 印出 `Order() is deprecated, use StockOrder() or FuturesOrder()` 的警告——目前還能用，但下次 SDK 大版本升級時這個可能會變成下一個要修的坑，先記一筆。
