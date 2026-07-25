# Refactor Librae: Decouple into a Standalone Backtest Engine

> 狀態：Phase 0/1/3/4 已落地（一次做完，2026-07-25）；Phase 2 併入 Phase 4 一併完成；Phase 5（repo 工程化基礎設施）2026-07-25 一併完成
> Commits：`8d94a27`（Phase 0/1/3/4）、`fe1b125`（README/architecture.md 整合）、`e8ca1ea`（Phase 5：ruff + CI + Python 版本矩陣）
> 範圍：librae/core, librae/live, librae/backtest, librae/config, librae/cli, db, brokers
> 建立日期：2026-07-25
> 依據：[refactor_librae_api](refactor_librae_api.md)（沿用其 RunConfig/引擎 wiring 基礎，本計劃在其上做邊界切割）

## 實作狀態（2026-07-25）

| Phase | 狀態 | 備註 |
|---|---|---|
| 0 — Repo/資料夾改名 | ✅ | GitHub + 本地資料夾 → `librae`，`.venv` 重建 |
| 1 — Telegram 注入洞 | ✅ | `LiveTrader.__init__` 新增 `notifier=_UNSET` sentinel；`librae/notifications/`、`librae/config/notification.py` 整個搬到頂層 `notifications/`；`notifications/telegram.py` 不再依賴 `brokers.base.CredentialConfig`（自帶 `from_env`）；**額外發現並修復**：`librae/live/executor.py` 對 `TelegramAdapter` 是模組層級（非 lazy）import，改到 `TYPE_CHECKING` 區塊,否則搬完 notifications 後 `import librae.live.engine` 會直接炸掉 |
| 2 — 資料契約反轉 | ✅（併入 Phase 4） | 唯一無條件 import `strategies.module.data.ohlcv` 的 `run_backtest_generic` 隨 `cli.py` 一併搬出 librae |
| 3 — 市場設定開放 | ✅ | `get_market(name, markets=)` 與 `CostModel.from_config(cfg, markets=)` 新增可選的外部 registry 參數，繞過套件內建 `markets.yaml` |
| 4 — cli.py 搬遷 + 依賴瘦身 | 🟡 部分 | `librae/cli.py` → `orchestration/cli.py` 已完成；**pyproject.toml 依賴瘦身跳過**——複查發現 `ccxt` 也被 `strategies/module/data/funding.py`（非 optional）使用、`psycopg2-binary` 是這個共用 monorepo pyproject.toml 的預設工作流程依賴，現在移到 optional 會讓 `uv sync --extra test` 這種現有預設安裝路徑悄悄漏掉 db/crypto 支援。等 librae 真正物理獨立成自己的 pyproject.toml 時再做這步 |

驗證：`pytest` 569 passed / 1 skipped；手動驗證 `LiveTrader(cfg=cfg_no_db, adapter=fake, notifier=None, cost_model=CostModel.zero())` 建構過程中攔截 `brokers`/`db`/`notifications` import 全部零觸發；`get_market()`/`CostModel.from_config()` 外部 market 注入驗證通過。

## 目標

把 librae 縮回「回測引擎本身」：策略研究、資料抓取、db 落地、broker 下單、通知全部退出 librae 的範疇，只留 `Backtest` / `LiveTrader` / `BaseStrategy` / cost model / market config 這組核心 API。db、broker、UI 全部改為透過介面注入的**擴充點**，目前 repo 內的 `db/`、`brokers/` 實作直接作為第一個「擴充範例」，不用重寫。終局目標：任何使用者（單資產或多資產策略）都能單獨 `pip install` librae，不必連帶處理 timescaledb/shioaji/ccxt 這些依賴。

---

## Phase 0 — Repo/資料夾改名（與內部解耦工作正交，可隨時做）

GitHub repo 與本地根目錄從 `quant-strategy-lab` 改名為 `librae`。**`strategies/` 先保留在原地不搬**——用真實策略跑過一輪，確認 Phase 1-4 的改動沒破壞任何東西後，才把實驗腳本移出。這個順序跟本計劃「邏輯獨立比物理搬遷優先」的原則一致。

**技術風險確認**：
- Python import 吃的是 `librae/` 這個 subpackage 資料夾名稱，跟 repo 根目錄名稱無關，改名不影響任何 import path。
- `.github/workflows/`、`deploy/` 未硬編碼 repo 資料夾名稱，CI/部署不受影響。
- Repo 目前是 private，改名期間「repo 叫 librae 但內含整個研究 monorepo（strategies/db/brokers/app/deploy）」的中繼狀態不會誤導外部使用者。
- 需同步修改的純文字內容：`pyproject.toml`（`name`, `Repository`/`Issues` URL）、`README.md`、`scripts/check_heartbeat.py` 的 cron 範例路徑註解、少數 `docs/` 文件內的路徑提及。

**佈局決策**：保留 `librae/librae/core/...` 疊字平舖佈局（repo 根目錄 `librae` 下維持現有 `librae/` package 資料夾），不改成 `src/librae/`——改動最小，且疊字佈局在 Python 生態中是常見、可接受的模式（如 `requests/requests`）。

---

## 修正一個前提：dst-tidal 的「client-local」其實是快的那條路

複查 [dst-tidal](https://github.com/awwesomeman/dst-tidal) 後，實際情況與「client local 設計比較慢」的印象相反：

```
預設架構（慢）：
tidal (client) ──ZMQ REQ/REP──▶ tidal_server ──HTTP REST──▶ tidal_service (Flask) ──K8s API──▶ tidal_operator

in-process 模式（快，v1.2.12 新增）：
tidal (client) 直接在自己 process 內建構 TidalServer，完全跳過 ZMQ + K8s
```

官方 benchmark（`tidal-docs/docs/changelog/1.2.12.md`）：

| Mode | 引擎 | 耗時 |
|---|---|---|
| K8s（預設，client/server 分離） | python @ 1.2.10 | 2278.75s |
| in-process（單機，無 ZMQ/K8s） | python | 10.95s ~ 13.53s |

**慢的是 client/server + K8s 這層分離架構本身**（每次操作都要走 ZMQ round-trip，再疊 HTTP + K8s API 兩層），不是「client 在本機跑」這件事。反而「in-process / client-local」正是他們事後為了解決效能問題才補上的**逃生門**——`CLAUDE.md` 甚至把「熱路徑效能：禁止在 `account.step()` 回測迴圈中引入新的逐筆 RPC 調用」列為明文禁止事項，另外開了 `backtest-performance-optimization` spec（Snapshot 推送、Batch Orders、Local Quote DB）去彌補分離架構帶來的效能債。

換句話說：dst-tidal 的歷史本身就是「先蓋了完整的 client/server/K8s/operator 分散式架構 → 發現熱路徑效能被 RPC 拖垮 → 花額外工程量做 in-process 模式和 batching 補救」。這是一個過度設計的前車之鑑，不是要抄的架構。

## 借鏡的是核心精神，不是分散式架構

**要參考的**：
1. Client 與核心引擎之間有清楚的介面邊界（`tidal` 只是薄 API，業務邏輯在 `tidal_server`）——對應到 librae 就是「`Backtest`/`LiveTrader` 只認 Protocol，不認具體的 db/broker 實作」。
2. 明確的資料格式契約（MultiIndex `instrument, datetime` + 固定欄位）——讓「餵資料進來」這件事跟資料來源無關。
3. local-first 預設：沒有外部基礎設施也能跑最小可行的回測（他們的 `in_process=True`，對 librae 而言應該是**唯一**模式，不是 opt-in 的第二選項）。

**明確不做的**：
- 不做 ZMQ/RPC client-server 分離——librae 就是一個 in-process 呼叫的 Python library，這是唯一正確的預設，連 opt-in 的 server 模式都不需要規劃。
- 不做獨立 server process、不做 K8s operator、不做前端 dashboard 併入 librae package——這些是部署層/研究層的關注點，本來就該被移出 librae 範疇，不是換個方式重新引入。
- 不引入訊息佇列或跨 process 通訊——db/broker 都是進程內的函式呼叫（同步 I/O），效能瓶頸目前不存在，沒有先做分散式的理由。

---

## 現況耦合盤點（2026-07-25 複查，librae 完成一輪風控功能重構後）

複查發現實況比原始盤點樂觀：`Backtest` 引擎本體、`core/executor.py`（含新加的 circuit breaker/margin/liquidation/volume-participation 風控邏輯）已經是乾淨的零外部依賴；`LiveTrader` 也已經有相當程度的建構子注入（`adapter`/`order_adapter`/`cost_model` 皆可傳入，db 寫入透過 `_UNSET` sentinel callback + `cfg.no_db` 控制，未接基礎設施時完全不 import `db`）。剩下的耦合收斂到更小、更具體的幾個點：

| 檔案 | 依賴 | 狀態 |
|---|---|---|
| `librae/live/engine.py` `__init__` | 無條件建構 `TelegramAdapter`（只有 `dry_run=True` 才跳過，`no_db=True` 不影響） → `librae/notifications/telegram.py` → `brokers.base.CredentialConfig` | 🔴 **唯一無法繞過的耦合**——傳了自訂 adapter + `no_db=True` 仍會被迫 import `brokers` |
| `librae/live/engine.py` `__init__` 的 `elif`/`else` 分支 | `brokers.shioaji_adapter`/`brokers.crypto_adapter` | 🟢 只在 `adapter is None` 時觸發，使用者傳自訂 adapter 就繞過，不是實際阻塞點 |
| `librae/live/engine.py` 的 `_build_on_*` callback | `db.timescale_writer`（6 處） | 🟢 已經是 lazy import，`cfg.no_db=True` 時完全不會 import `db` |
| `librae/backtest/charts.py::plot_trades_by_run_id` | `db.timescale_reader` | 🟡 獨立的便利 wrapper，核心渲染函式（吃記憶體 DataFrame）本身乾淨，搬走這個 wrapper 不影響核心邏輯 |
| `librae/backtest/charts.py`（核心渲染函式） | `lightweight_charts`（硬編碼，無抽象層） | 🟡 新增依賴，對應「UI 可擴展性」目標但目前只有一種後端，YAGNI——先不抽象 |
| `librae/cli.py` | `db.timescale_reader/writer`, `strategies.module.data.ohlcv` | 🟡 純組裝層，`Backtest`/`LiveTrader` 本身已與此解耦，優先度降低——只影響用 `librae.cli` 的使用者，不影響直接 import 引擎類別的使用者 |
| `librae/config/market_config.py` | 無外部模組依賴，但 `get_market()` 綁死套件內建的 `markets.yaml` 路徑 | 🔴 未變動，Phase 3 原範圍不變 |

**結論**：Phase 1 的範圍從「幫 db/broker 補一層 Protocol」縮小成「幫 Telegram 補上跟 db 一樣的 `_UNSET` sentinel 注入」——現有的 callback-per-event 注入風格已經是對的方向，蕭規曹隨即可，不需要另立一個 `ResultSink`/`OrderExecutor` Protocol 抽象。

---

## 分階段計畫

### Phase 1 — 補 Telegram 的注入洞（範圍已縮小）

不新增 `ResultSink`/`OrderExecutor` Protocol——db 寫入的 `_UNSET` sentinel callback 模式已經達到相同效果，且更貼合現有 `refactor_librae_api.md` 的 data-driven callback 風格，直接沿用即可。實際要做的只有：

1. `LiveTrader.__init__` 新增 `notifier: Callable[..., None] | None | object = _UNSET` 參數，納入既有的 `callbacks` dict-driven resolve loop，跟 `on_bar`/`on_heartbeat` 同一套規則：明確傳值優先 → `cfg.no_db`（或新增對等的 `cfg.no_notify`）為 True 時給 `None` → 否則才 lazy import `librae.notifications.telegram` 建構預設值。
2. `librae/notifications/telegram.py` 對 `brokers.base.CredentialConfig` 的依賴拔掉，改自己定義最小型別（型別依賴方向本來就反了，跟 sentinel 注入是兩件獨立的事，可以先單獨修）。
3. `librae/notifications/` 整個搬出 librae，落到 repo 頂層的 `notifications/`（見下方專案架構）——與 `db/`、`brokers/` 同層級，作為 Notifier 的「擴充範例」。

`db/timescale_writer.py`、`brokers/*_adapter.py`、搬出後的 `notifications/telegram.py` 內部邏輯都不用動。

**驗收**：`LiveTrader(strategy, feature_fn, cfg=cfg_no_db_no_notify, adapter=fake_adapter)` 建構過程中無任何 `from db...` / `from brokers...` / `from librae.notifications...` import；用假 adapter + `notifier=None` 跑 live engine 單元測試，不連真實 DB/Telegram。

### Phase 2 — 資料契約反轉（範圍已縮小到 cli.py）

複查發現 `live/engine.py::_build_warmup_fetcher` 對 `strategies.module.data.ohlcv.get_ohlcv` 的呼叫也已經在同一套 `_UNSET` sentinel 機制下——`cfg.no_db=True` 或使用者自傳 `warmup_fetcher` 時就不會觸發。真正會無條件 import `strategies.module.data.ohlcv` 的只剩 `librae/cli.py::run_backtest_generic`。因為 Phase 1 已經把 `cli.py` 的優先度降低（它不影響直接 import 引擎類別的使用者），Phase 2 併入 Phase 4 一起處理即可，不需要獨立一個 phase。

保留原則：沿用 librae 現有 `Context.bars`（dict-of-DataFrame）的資料形狀作為正式契約並寫進 docstring，不引入 dst-tidal 的 MultiIndex 格式（沒有理由維護兩套資料形狀）。

**驗收**：`grep -r "strategies\." librae/core/ librae/backtest/ librae/live/` 無結果（`cli.py` 移出 librae 後自然滿足，見 Phase 4）。

### Phase 3 — 市場設定開放

`MarketConfig`/`load_market_configs` 保留 yaml 讀取（給預設範例用），但 `get_market()` 改成也能接受呼叫方已經 build 好的 `dict[str, MarketConfig]`，不強制套件內建路徑是唯一真相來源。不引入 enum（dst-tidal 的 `StockConfig.TW` 那種做法）——市場種類是開放集合，enum 只會逼使用者持續 fork 套件加成員，dataclass + 呼叫方自組字典就夠。

**驗收**：不改套件內任何檔案的前提下，能在套件外部組出一個新市場的 `MarketConfig` 並餵進 `Backtest`。

### Phase 4 — cli.py 搬遷 + 依賴瘦身

`cli.py`（本質是整合層，同時知道 db schema、strategies 目錄結構、引擎內部 API）整支移出 `librae/` package，落到 repo 根目錄的 orchestration 層。`pyproject.toml` 的 `psycopg2-binary`、`ccxt` 從必要依賴移到 optional group（`shioaji`/`ib-async` 已經是 optional）。

物理搬成獨立 repo 是否要做、何時做，留到 Phase 1-3 跑穩之後再評估——邏輯獨立比物理搬遷優先。

### Phase 5 — Repo 工程化基礎設施（不在原計劃範圍，2026-07-25 一併完成）

跟「解耦」本身無關，但都是支撐「librae 未來要能被單獨拿去用」這個終局目標的基礎工程：

- **文件整合**：`librae/README.md` 刪除，內容依性質拆到根目錄 `README.md`（精簡版，一段描述 + 一個範例）與 `architecture.md`（完整架構/API/設計決策，跟既有的 Broker Adapter/資料存取層設計並列）。順便修掉解耦時留下的 `librae.cli`/`librae.notifications` 路徑殘留引用。
- **ruff lint + format**：`pyproject.toml` 新增 `[tool.ruff]`，範圍涵蓋 `librae/`、`notifications/`、`orchestration/`、`db/`、`brokers/`、`app/`、`scripts/`、`tests/`（`strategies/` 排除，遷移中不值得先清）。實際發現的 lint 問題直接修（import 排序、`datetime.UTC`、`zip(strict=True)`、`ibkr_adapter.py` 一段永遠等價的死 if/else 分支、缺漏的 `TYPE_CHECKING` import 等），不是加規則忽略——唯一例外是 `librae/__init__.py` 的 import/`__all__` 排序，那是刻意照 API 分類分組，保留 per-file-ignore。
- **CI**：三個 workflow（`core-tests`/`tw-live-tests`/`us-live-tests`）統一改用 `uv sync --frozen`（原本是裸 `pip install`，跟本機開發脫鉤），Python 版本從 3.11（低於 `requires-python>=3.12`，先前的 bug）改成 3.12/3.13/3.14 矩陣，事前已用 `uv run --python X pytest` 逐版本本機驗證過（含 `tw_live`/`us_live` 標記測試）。新增 `lint.yml`（ruff check + format --check）與 `us-live-tests.yml`（`us_live` marker 原本沒有對應的 CI job）。
- **本地 pre-commit hook**：`.githooks/pre-commit` 鏡射 CI 的 lint 檢查，`git config core.hooksPath .githooks` 啟用。
- **`LICENSE`**：`pyproject.toml` 宣稱 MIT 但沒有實體檔案，補上。

**明確跳過**（見對話記錄，非本次疏漏）：`CHANGELOG.md`（沒有版本發布可掛，`docs/decisions/`+`docs/plans/` 已覆蓋）、mkdocs（使用者已排除）、`mypy`/commitizen/PyPI release pipeline/多 OS 測試矩陣（等 librae 真正物理獨立成自己的套件時才有意義）。

---

## 實際落地的專案架構（2026-07-25）

Phase 1 執行時發現 `_UNSET` sentinel callback 模式已經達到跟 Protocol 抽象一樣的效果（見上方「現況耦合盤點」的結論），所以**沒有新增 `sinks.py`**——下面是實際落地的樣子，取代本節原先規劃但未採用的 `ResultSink`/`OrderExecutor`/`Notifier` Protocol 設計：

```
librae/                          # 零 db/broker/strategies 依賴
├── __init__.py                  # 對外 API：Backtest, LiveTrader, BaseStrategy,
│                                 # Context, Position, Action, RunConfig, MarketConfig
├── core/
│   ├── strategy.py              # BaseStrategy / Context / Position / Action — 不動
│   ├── executor.py              # process_actions — 不動
│   ├── cost_model.py            # from_config() 新增 markets= 參數
│   ├── run_config.py            # 不動，純參數容器
│   ├── metrics.py               # 不動
│   └── utils.py
├── backtest/
│   ├── engine.py                # Backtest — 資料由呼叫方傳入
│   ├── charts.py                # 核心渲染函式吃資料參數；plot_trades_by_run_id 是唯一還碰 db 的便利 wrapper
│   └── schema.py
├── live/
│   ├── engine.py                # LiveTrader — adapter/order_adapter/cost_model/notifier 皆為建構子注入
│   │                             # （notifier=_UNSET sentinel，跟 db callback 同一套模式，不是獨立 Protocol）
│   └── executor.py              # TelegramAdapter import 移到 TYPE_CHECKING
└── config/
    ├── market_config.py         # get_market(name, markets=) 可接外部注入的 registry
    └── symbols.py

# --- librae 之外，同一個 repo 內（物理搬遷前的中繼狀態）---

db/                               # 不搬動位置；LiveTrader 的 db callback 範例（timescaledb）
brokers/                          # 不搬動位置；adapter/order_adapter 的範例（shioaji/ccxt/ibkr）
notifications/                    # 新頂層目錄；從 librae/notifications/ 搬出，notifier 的範例（telegram）
orchestration/
└── cli.py                        # 原 librae/cli.py：build_config / run_dispatch / with_dedup_check
strategies/                       # 策略研究層：因子/regime/資料抓取，不變（下一步遷移目標）
scripts/                          # 一次性資料腳本，不變
```

**判斷依據**：`db/`、`brokers/` 保留原本的頂層位置不搬——它們本來就已經在 librae 之外，搬動只是換名字沒有實質效益。真正需要動的是「被夾在 librae package 裡面」的兩個異類：`librae/cli.py`（整合層，不是引擎）和 `librae/notifications/`（跟 db/brokers 性質相同的外部擴充點，只是historically被放錯位置）。

---

## 不動的東西

- `Backtest`/`LiveTrader` 主迴圈邏輯
- `librae/core/strategy.py`、`cost_model.py`、`executor.py`
- 不引入任何 client-server/RPC/K8s 相關基礎設施

---

## 影響檔案

| 檔案 | 改動 | Phase |
|---|---|---|
| `librae/live/engine.py` | 新增 `notifier=_UNSET` sentinel 參數 + `_build_notifier()`；移除頂層 telegram/notification import | 1 |
| `librae/live/executor.py` | `TelegramAdapter` import 移到 `TYPE_CHECKING`（原本是模組層級硬依賴，計劃原文沒抓到這處） | 1 |
| `notifications/`（新頂層目錄） | **搬入** — 原 `librae/notifications/telegram.py` + `librae/config/notification.py`（改名 `config.py`），移除對 `brokers.base` 的依賴 | 1 |
| `librae/config/market_config.py` | `get_market(name, path=, markets=)` 新增 `markets` 參數 | 3 |
| `librae/core/cost_model.py` | `CostModel.from_config(cfg, override=, markets=)` 新增 `markets` 參數並傳給 `get_market` | 3 |
| `orchestration/cli.py`（新目錄） | **搬入** — 原 `librae/cli.py` 整支移出，含 `run_backtest_generic` 的 `strategies.module.data.ohlcv` 呼叫 | 2, 4 |
| `pyproject.toml` | 只更新 `packages.find` include（加 `notifications*`/`orchestration*`）；依賴瘦身跳過，見上方實作狀態 | 4 |
| `tests/notifications/`, `tests/orchestration/` | 對應搬移 `tests/config/test_notification.py`、`tests/config/test_cli.py`，`tests/config/` 已空並刪除 | 1, 4 |

---

## 驗證

- [x] `pytest tests/ -q` 全部通過（569 passed, 1 skipped）
- [x] `grep -r "from db\.\|from brokers\." librae/` 無結果
- [x] `grep -r "strategies\." librae/` 無結果（`cli.py` 已移出）
- [x] `librae/notifications/`、`librae/config/notification.py`、`librae/cli.py` 均不存在
- [x] `LiveTrader(cfg=cfg_no_db, adapter=fake, notifier=None, cost_model=CostModel.zero())` 建構時攔截 import，確認 `brokers`/`db`/`notifications` 零觸發
- [x] `get_market("x", markets={...})`、`CostModel.from_config(cfg, markets={...})` 外部市場注入驗證通過
- [ ] `librae/` 目錄下 `import librae` 不觸發任何 db/broker 相關 import error（即使沒裝 `psycopg2`/`ccxt`）——**未驗證**，因為 `psycopg2`/`ccxt` 仍是 base dependencies 尚未拆分（見 Phase 4 備註）

---

## 風險 / Watch-outs

- `live/engine.py`（本計劃撰寫時 737 行）改動面最大，db 呼叫散落在 6 個方法，需逐一核對參數對得上 Protocol 簽名。
- Phase 2/4 會改變現有 pipeline（研究→部署）的接線方式，是一次性遷移成本，但不影響回測邏輯正確性。
- 不要因為看到 dst-tidal 有 server/K8s 層就聯想「未來規模大了會需要」——目前沒有任何跨機器並行回測的實際需求，這類基礎設施要等真的出現瓶頸（例如需要跑數千組參數網格搜尋）才評估，而且屆時的答案更可能是「多進程 + 共用唯讀資料」而不是重新蓋一套 RPC server。
