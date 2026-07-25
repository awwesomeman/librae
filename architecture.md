# Architecture & Naming Conventions

> **文件定位**：這是一份**持續更新的現況文件**，反映系統目前的架構與命名慣例，隨程式碼演進直接修改本檔。
> 這與 `docs/decisions/`（決策當下存證、寫下後不回溯修改）性質相反 —— 本檔只承載「現在是什麼」，
> 命名規則背後「為什麼」的決策脈絡留在對應的 decision 文件，本檔用連結交叉引用。
>
> 新增/修改 table、column、`db/` 讀寫函數時，**必須同步更新本文件**。若命名規則本身改變（而非新增條目），
> 視情況在 `docs/decisions/` 補一份新的 decision 記錄「為什麼改」。

## 部署拓樸（本機 / VM / 交易所）

```mermaid
flowchart LR
    subgraph local["本機（開發 + 認證 + 回測）"]
        code["程式碼 + 策略開發"]
        api["打 API（ccxt / Shioaji，唯讀 key）"]
        cache["OHLCV/因子 cache"]
        grafana_local["Grafana :3000（可選）"]
    end

    subgraph vm["VM（Tailscale mesh，只跑 Docker，無 repo）"]
        db[("TimescaleDB :5432")]
        grafana_vm["Grafana :3000"]
        trade["trade 容器（sim 或 live，選用）"]
    end

    exchange(["交易所 API<br/>Binance / Shioaji"])
    user(["使用者裝置<br/>（任何地方）"])

    code --> api --> cache
    cache -- TIMESCALE_DSN --> db
    grafana_local -- 查詢 --> db
    grafana_vm -- 查詢 --> db
    trade -- 查詢/寫入 --> db
    trade -- "real order<br/>（交易 key 只在 VM）" --> exchange
    user -- "帳號密碼<br/>（不限 IP）" --> grafana_vm
```

- **本機**：策略開發、回測、資料抓取都在這裡；OHLCV 快取進 DB 避免重複打 API。本機的 Shioaji/Binance key 一律唯讀，不下真單。
- **VM**：只裝 Docker，跑 TimescaleDB + Grafana，可選常駐一個 trade 容器做真實下單（Binance/Shioaji live）。交易 key（含 Shioaji CA 憑證）只存在 VM 上，細節見下方「VM 部署與策略管理」。**不 clone repo**，靠 Tailscale 私有網路連線，密碼/程式碼都不落地到 VM 之外的地方。
- **Grafana**：本機開（幾乎不吃資源，直接查遠端 DB）或放 VM 上皆可。

## 系統分層概覽

```
brokers (券商/交易所 adapter)  →  librae (core → backtest / live)  →  db (timescale_writer / timescale_reader)  →  Grafana / Streamlit
```

- `brokers/`：每個券商/交易所一個 adapter（`ShioajiAdapter`、`CryptoAdapter`、`IBKRAdapter`），提供 `fetch_ohlcv` / `place_order` / `get_position` / `info`，供 live engine 抓資料與下單。設計細節見下方「Broker Adapter 設計」。
- `strategies/module/data/`：策略/回測要用的市場資料與第三方因子的唯一存取層，`get_ohlcv()`/`get_factor()` 統一走 DB-first + 缺口補值。設計細節見下方「資料存取層設計」。
- `librae/core/`：策略執行的共用邏輯（`strategy.py` 定義 Position/Action/Fill，`executor.py` 定義 TradeResult/OrderEvent 與撮合邏輯），backtest 與 live 共用。
- `librae/backtest/engine.py`：逐 bar 回測引擎，產出 `BacktestOutput`（`librae/backtest/schema.py` 定義的 DB 持久化用 dataclass：RunMetadata/EquityCurvePoint/OrderEventRecord/StrategyMetrics）。
- `librae/live/engine.py`：sim/live 模式的即時輪詢引擎，同一份 executor 邏輯，即時寫入 DB，資料/下單透過 `brokers/` adapter。
- `db/timescale_writer.py` / `db/timescale_reader.py`：唯一的 DB 存取層，上層一律透過這裡讀寫，不直接下 SQL。
- Grafana（`app/grafana/generate_dashboards.py` 產生 JSON）與 Streamlit：下游視覺化，直接查詢 TimescaleDB。

分層細節與四層分離的決策脈絡見 `docs/decisions/2026-03-26-platform-architecture.md`（現況已用 librae 取代文件中提到的舊執行層）。

## Broker Adapter 設計（`brokers/`）

- 每個券商/交易所一個扁平 adapter class（`ShioajiAdapter`、`CryptoAdapter`），**duck-typed，不繼承共同 ABC**。共同方法簽章：`fetch_ohlcv(symbol, timeframe, ...) -> pd.DataFrame`、`place_order(signal: dict) -> dict`、`get_position(symbol) -> dict`、`info() -> AdapterInfo`。
- `brokers/base.py` 只提供兩個真正共用、逐字相同的部分：`AdapterInfo`（靜態 metadata）與 `CredentialConfig.from_env(prefix)`（env var 讀取慣例 `{PREFIX}_{FIELD}`，`prefix` 由呼叫端指定，例：`SHIOAJI_API_KEY`、`BINANCE_API_KEY`）。`CryptoAdapter`/`CryptoCredentials` 本身跟交易所無關（靠 `exchange_id` 選 CCXT 後端），目前只接了 Binance，用 `BINANCE_*` 當 prefix；之後加第二個 crypto 交易所，走同一個 class、換一個 prefix（例如 `OKX_*`）即可，不用改共用邏輯。
- OHLCV 回傳統一 schema：`[ts, open, high, low, close, volume]`，`ts` 為 UTC-aware datetime；timeframe 字串轉換共用 `librae/core/utils.py`（`interval_to_timedelta` 等），不在各 adapter 重複實作。
- 需要型別約束時用 `typing.Protocol`，**在呼叫端就近宣告最小介面**，不做涵蓋全部能力的共用介面 —— 例如 `librae/live/executor.py` 的 `OrderAdapter` Protocol 只宣告 `place_order`，因為 executor 只用到這個方法。
- 曾嘗試以 async ABC 分層（`MarketDataAdapter`/`OrderAdapter`/`AccountAdapter`）搭配 `MarketHub` 統一 dispatch（見 `docs/decisions/2026-03-26-market-adapter-architecture.md`），因 Shioaji（stateful login+CA）與 CCXT（stateless per-call REST）的 auth 模型差異太大、且無 adapter 真正使用該分層而移除；**現況以扁平 duck-typed class 為準，不要重新引入跨券商的共用階層**。

## 資料存取層設計（`strategies/module/data/`）

兩個獨立的分類軸，分別解決兩個不同的「會變亂」問題：

**軸一：`providers/` vs 概念檔案**——`strategies/module/data/providers/` 只放**純 API/SDK client**（目前唯一案例：`providers/finmind.py`），零商業邏輯，一個外部資料供應商一個檔案；概念檔案（`funding.py`、`open_interest.py`、`cross_asset.py`、`tw_futures_chip.py`、`tw_market_flow.py`...）留在 `strategies/module/data/` 最上層，依「這個因子代表什麼」命名，不是依「從哪個 API 來」命名。同一個 provider 可以被多個概念檔案 import（`tw_futures_chip.py` 跟 `tw_market_flow.py` 都用 `providers/finmind.py`，因為兩者回答的問題不同：前者是「TX 期貨/選擇權籌碼」，後者是「全市場現貨資金流」），加新資料集只需要在對應的概念檔案加一個 fetcher，不會讓 provider client 越長越肥。

**軸二：`get_ohlcv()` vs `get_factor()`**——两者是同一套 DB-first + 缺口追蹤引擎（`compute_coverage_gaps` 共用，見「資料流」小節），差別只在快取的表跟粒度：`get_ohlcv()`（`ohlcv.py`）固定 schema `[timestamp, open, high, low, close, volume]`，`data_source` 對應到一個 `register_ohlcv_fetcher` 註冊的 fetcher（`binance_spot`/`shioaji`/`ibkr`...）；`get_factor()`（`factors.py`）是任意外部時序，固定 schema `[timestamp, value]`，`factor_name` 對應到一個 `register_factor_fetcher` 註冊的 fetcher，寫進共用的 `external_factors` long table（見「資料庫設計規範」），新因子不用 migration。**只有真的有抓取成本（API call、rate limit）的資料才進 `get_factor()`**——從已快取 OHLCV 現場算出來的特徵（`cross_asset.py`、`regime.py`）不算,它們沒有「缺口」的概念,直接算就好,不要為了統一而硬塞進快取引擎。

**Frequency 命名規範**：`register_factor_fetcher(..., frequency=...)` 用的字母代碼（`M5`/`H1`/`D1`/`W2`/`MN3`...）不是為 factor 另外發明的一套，是重用 `librae/core/utils.py` 原本為 OHLCV K 棒週期設計的 `{PREFIX}{N}` canonical 格式（`to_canonical()`/`to_ccxt()` 互轉用）——`M`=分鐘、`H`=小時、`D`=天、`W`=週、`MN`=月（兩個字母，因為 `M` 被分鐘用掉了），regex parse 任意 `{letter}{number}` 組合，不用查表。目的是全 repo 只維護一套頻率字母代碼，K 棒週期跟因子更新頻率共用同一套詞彙。唯一的例外是 `FREQUENCY_IRREGULAR`（`factors.py`）——沒有固定格點的真實事件資料（股利、內部人交易）用這個 sentinel，不硬套進 M/H/D/W/MN 規則。權威定義只在 `librae/core/utils.py` 開頭的註解區塊，這裡不重複列完整規則。

**Path A vs Path B 動詞命名**：`get_factor()`/`register_factor_fetcher()`（Path A，DB-first + API gap-fill）跟 `load_snapshot_factor()`/`collect_snapshot_factor()`（Path B，見上方「什麼時候該走 Path B」）動詞故意不對稱，不是命名不一致：`get_`/`load_` 都回傳資料（DataFrame），但 `get_` 隱含「DB 沒有就自動打 API 補」，`load_` 是純 DB 讀取、永遠不會觸發 API——這個差異值得用不同動詞標出來。`collect_` 則回傳「寫入筆數」（int），不是資料本身，所以不能跟 `get_`/`load_` 用同一個動詞；`collect_`/`load_` 這組沿用的是 `us_chip.py` 原本就有的既有慣例（`collect_short_interest()`/`load_short_interest()`），每個 Path B 概念檔案都跟著用（`collect_social_mentions`/`load_social_mentions`...）。

**`attach_*_features(ohlcv, ..., start, end) -> DataFrame` 慣例**：每個概念檔案暴露的组合函式，把該檔案管的因子掛到一份 OHLCV DataFrame 上。一律用 `utils.py` 的 `merge_asof_backward()`（backward asof-merge）以及 `attach_or_zero_fill()`（merge 或補 0，因子還沒查到資料時的統一慣例）——backward-only 是防前視偏誤的關鍵：外部因子只在「已公布」的時間點才能被那個時間點之後的 bar 看到,絕對不能變成 forward-fill 或誤用 nearest。這兩個 helper 只寫一次、所有 `attach_*_features` 共用，不要各自重新發明 merge 邏輯。

**Pseudo-symbol 慣例**：`get_factor()`/`external_factors` 的 cache key 一定要有 `symbol`，但有些因子是全市場級別、沒有真實對應的可交易標的（例如 `tw_market_flow.py` 的全市場融資融券餘額）——這種情況用一個固定的假 symbol（目前是 `"TWSE"`）當 cache key，純粹是資料庫層的識別用途，**不是** `symbols.yaml` 註冊的真實交易標的，也不會出現在 `librae/config/symbols.py` 的 registry 裡。查 DB 時看到 `symbol='TWSE'` 的列，代表的是市場級別聚合資料，不是某個叫 TWSE 的可交易商品。

**Provider 自己的 id 對應**：外部資料源常有自己的一套代號（FinMind 的 `TX`/`MTX`/`TMF`/`TXO`），跟 `symbols.yaml` 的專案 symbol（`TXFR1`/`MXFR1`/`TMFR1`）不是同一套。這層對應目前是各消費該 provider 的概念檔案自己維護一個小 dict（例如 `tw_futures_chip.py` 的 `_FUTURES_ID_MAP`），**不進 `symbols.yaml`**——`symbols.yaml` 是「這個 symbol 在哪個市場、用哪個 data_source、契約經濟性質」的 registry，不是「每個 provider 怎麼稱呼這個 symbol」的翻譯表；後者若某天多到需要跨檔案共用，才考慮抽成獨立模組，現在只有 2-3 個 entry 不值得為此加一層抽象。

**什麼時候開新的概念檔案 vs 加進既有檔案**：同一個問題領域（「這個因子在回答什麼」）加進既有檔案；不同問題領域即使來自同一個 provider 也分開檔案（`tw_futures_chip.py` 的「期貨籌碼」跟 `tw_market_flow.py` 的「現貨資金流」是兩個不同問題）。單一檔案超過約 200 行、或混進第二個問題領域，是該拆的訊號。

## 回測引擎設計（`librae/`）

量化回測與即時交易引擎。提供策略執行、持倉管理、成本模擬、績效計算的完整框架。**回測、模擬、實盤共用同一份策略，零修改。**

### 架構

```
librae/
├── core/                     共用 domain model（純計算，無 I/O）
│   ├── strategy.py           BaseStrategy, Action, Context, Position, PositionState, Fill
│   ├── executor.py           make_fill, process_actions, calc_trade_pnl, close_position, scale_into_position, reduce_position, liquidate_all
│   ├── cost_model.py         CostModel（手續費 / 滑價 / 稅 / 合約乘數 / 保證金）
│   ├── metrics.py            compute_all（QuantStats adapter）
│   ├── run_config.py         RunConfig — 統一執行參數（frozen dataclass）
│   └── utils.py              generate_run_id, infer_timeframe, to_ccxt, to_canonical
│
├── backtest/                 回測 runtime
│   ├── engine.py             Backtest — bar-by-bar 執行 + build_output()
│   ├── schema.py             BacktestOutput, RunMetadata, StrategyMetrics, OrderEventRecord
│   └── charts.py             plot_trades — lightweight-charts 疊 order_events 進出場點（純渲染，不重算，本地研究用；[extra: viz]）
│
├── live/                     即時 / 模擬 runtime
│   ├── engine.py             LiveTrader — polling loop + 信號偵測
│   └── executor.py           LiveExecutor（sim 通知 / live 下單）
│
└── config/                   設定管理
    ├── markets.yaml          市場參數（成本模型、tick_size、乘數、保證金率）
    ├── market_config.py      MarketConfig dataclass + load helpers
    └── symbols.py            symbol → market/data_source 對應

# librae 之外，跟 db/、brokers/ 同層級的擴充範例（可替換，見下方「依賴方向」）
notifications/                Telegram 推播（TelegramAdapter + TelegramCredentials）
orchestration/cli.py          共用 CLI parser + config YAML 合併（build_config/run_dispatch）
```

### 依賴方向

```
backtest/ ──→ core/
live/     ──→ core/
```

`backtest/` 和 `live/` 之間無直接依賴，共用邏輯全部在 `core/`。`db`/`brokers`/`notifications` 都不是 `librae` 的必要依賴——`LiveTrader` 用 `adapter`/`order_adapter`/`cost_model`/`notifier` 建構子參數 + `cfg.no_db` 控制是否需要它們，未注入時走 lazy import 的預設實作（`brokers.*`/`db.timescale_writer`/`notifications.telegram`），明確傳入或 `cfg.no_db=True` 時完全不會 import 這些套件（見 `docs/plans/refactor_librae_decouple.md`）。

### 執行流程（策略 → 引擎 → 輸出）

跟下方「資料流」小節是兩件事：那裡講的是 DB 表之間的讀寫管線，這裡講的是單次 run 內、策略程式碼到輸出結果的呼叫順序。

```
策略 ETL (utils.py)  →  DataFrame (MultiIndex + 信號欄位)
                              ↓
策略邏輯 (strategy.py)  →  on_bar(ctx) → list[Action]
                              ↓
引擎 (engine.py)     →  make_fill / close_position → Fill / TradeResult
                              ↓
輸出 (build_output)  →  BacktestOutput (metrics + equity_curve + trades)
```

### 使用方式

#### 回測 (backtest)

```python
from librae import Backtest, BaseStrategy, Action, Context, RunConfig

# 1. 定義策略
class MyStrategy(BaseStrategy):
    def on_bar(self, ctx: Context) -> list[Action]:
        if ctx.positions.get(ctx.symbol):
            if ctx.bar.get("exit_signal"):
                return [Action(type="close", symbol=ctx.symbol)]
            return []
        if ctx.bar.get("entry_signal"):
            return [Action(type="long", symbol=ctx.symbol)]
        return []

# 2. 跑引擎（通常由 orchestration.cli.build_config() 建立 RunConfig）
df = fetch_and_prepare(symbol, months)          # 你的 ETL
bt = Backtest(data=df, strategy=MyStrategy(), cfg=cfg)
bt.add_benchmark(df.xs(symbol, level="symbol")["close"])
bt.run()

# 3. 取得結果
output = bt.build_output()                      # BacktestOutput
```

**資料格式**：MultiIndex DataFrame `(symbol, datetime)` + OHLCV + 自訂特徵欄位。

#### 多資產 / 選股策略

引擎本身是 portfolio-level 設計（`positions` 是 `dict[symbol]`，`equity_curve`/`metrics` 皆為組合層級），`on_bar()` 可在同一根 bar 回傳多個不同 symbol 的 `Action`，不需改動 engine/executor/schema。唯一要注意：`Action.quantity=None` 預設用光可用現金（單資產便利預設），同一 bar 開多檔部位時必須自行算好每檔 `quantity`（見 `strategy.py` 中 `Action.quantity` docstring），否則第一個 Action 會吃光現金。

#### 本地看進出場點 (trade chart)

`pip install -e ".[viz]"` 後使用。純渲染 `build_output()` 已算好的 `order_events`，不重新模擬/計算，數字保證跟 `strategy_performance`/Grafana 一致（SSOT 見上方「多資產 / 選股策略」段落）。

```python
from librae.backtest.charts import plot_trades, plot_trades_by_run_id

ohlcv = df.xs(symbol, level="symbol")            # 單一 symbol 的 OHLCV
plot_trades(ohlcv, output.order_events, symbol)  # 剛跑完回測，手上已有 output

plot_trades_by_run_id(run_id)                    # 或者：不重跑回測，直接從 DB 讀已落地的 run
```

`plot_trades_by_run_id` 讀的是 `db.timescale_reader.load_trade_events`/`load_ohlcv`——跟 Grafana 同一份 `trade_events`/`ohlcv` 表，同源保證不 drift。

#### 風控 (risk controls)

引擎層級強制，策略無法繞過；三者皆預設關閉（`None`）。backtest/live 共用同一份 `core.executor.liquidate_all`/`_cap_fill_to_notional`/`_cap_fill_to_volume`。

```python
cfg = RunConfig(..., params={
    "max_position_pct": 0.3,             # 單一部位 notional 上限 = 30% 最新已知權益
    "max_drawdown_pct": 0.2,             # 權益從高點回落 20% -> 全平倉並永久停止進場
    "max_volume_participation_pct": 0.1, # 單筆成交量上限 = 10% 該根 bar 的 volume
})
```

- `max_position_pct`：新倉/加碼皆會被裁量（裁量後重算 commission/slippage/tax），不是直接拒絕。
- `max_drawdown_pct`：觸發後呼叫 `liquidate_all()` 全平倉，並停止呼叫策略 `on_bar()`（live 仍持續 polling/監控，只是不再進場）；一次觸發即永久生效，需重啟該次 run。
- `max_volume_participation_pct`：只限制單筆成交（新倉/加碼），不是累加 vs 部位大小；跟 `max_position_pct` 一樣是裁量不拒絕。只作用於進場 —— 出場（策略平倉、停損/停利、force close、回撤熔斷平倉）不受此限制。
- 成交量感知的滑價（`CostModel.impact_coef`）跟這個開關無關、預設也是關閉：只要有成交量資料傳入，且該市場/symbol 的 `impact_coef > 0`（在 `markets.yaml`/`symbols.yaml`/`cost_overrides` 設定），滑價就會隨單筆成交佔該 bar volume 的比例線性放大，無論有沒有設定上限。

#### 保證金 / 強平模擬

`CostModel.maintenance_margin_rate`（預設 0 = 關閉，跟 `impact_coef` 同一套「屬於市場/商品，不是 `cfg.params`」的設定方式，走 `markets.yaml`/`symbols.yaml`/`cost_overrides`）。設定後 `resolve_stop_exit`（backtest/live 共用）在每根 bar 都會檢查部位是否觸及 `CostModel.liquidation_price(entry_price, side)` 算出的強平價，觸及就以 `REASON_LIQUIDATION` 強制平倉，用跟 `stop_price` 一樣的 gap-through 邏輯（缺口跳空時取較差的（強平價, bar open））。強平檢查優先於停損/停利——同一根 bar 兩者都觸發時，強平（交易所實際會執行的最保守結果）優先。

公式是簡化過的 isolated margin 近似值（忽略手續費/資金費率，維持這個引擎既有保證金模型的簡化程度）：多單 `entry*(1 + maintenance_margin_rate - margin_rate)`，空單 `entry*(1 - maintenance_margin_rate + margin_rate)`。現貨（`margin_rate=1.0`）不設 `maintenance_margin_rate` 就永遠不會觸發。

#### 對帳 (reconciliation, live only)

`LiveTrader.run()` 啟動時自動執行，`sim` 模式（無 `order_adapter`）為 no-op：

- **部位**（`_reconcile_positions`）：直接採信 broker 回傳的 `get_position()`，覆蓋本地 `self._positions`——部位方向/數量是無歧義的，錯誤的本地部位對訊號判斷是實際風險。
- **現金**（`_reconcile_cash`，目前僅 `CryptoAdapter`/CCXT 支援，其他 broker adapter 沒有 `get_balance()` 會被 duck-type 跳過）：只告警不覆蓋。落差超過 `LiveTrader.CASH_RECONCILE_TOLERANCE_PCT`（預設 1%，engine 常數而非 `cfg.params`）才發 Telegram alert，`self._cash` 永遠以本地帳本為準——broker 的 free/total 餘額語意會隨帳戶模式（現貨/合約/cross-margin）不同，貿然覆蓋可能讓本來正確的本地狀態被錯讀的數字污染。

#### 資料 staleness 偵測 (live only)

每個 poll cycle 都會檢查，跟上面對帳不同、不是只在啟動時跑一次。`_check_staleness` 比對最新一根 bar 的時間戳跟現在時間的差距，超過 `(LiveTrader.STALE_DATA_TOLERANCE_BARS + 1) * timeframe`（預設 tolerance=2，即 3 個 timeframe 沒有新資料）才發告警——`+1`是因為即使 feed 完全正常，一根已收盤 bar 的時間戳本來就會落後現在時間約 1 個 timeframe，這是預期的，不能當成 stale。純監控功能，不影響交易行為（不會 halt、不會擋新倉），所以是 always-on 的 engine 常數，跟 `CONSECUTIVE_ERROR_THRESHOLD` 同一套設計理由；跟後者的差別是 `CONSECUTIVE_ERROR_THRESHOLD` 只抓 fetch 拋例外的情況，這個抓的是 fetch 成功但資料不再更新（exchange API 靜默卡住）。edge-triggered：從新鮮轉 stale 才發一次，不會每個 cycle 洗版，資料恢復後會重新武裝、下次 stale 還會再告警一次。

#### 模擬監控 (sim)

```python
from librae.live.engine import LiveTrader

trader = LiveTrader(
    strategy=MyStrategy(),
    feature_fn=prepare_signals,     # 同一個 ETL pipeline
    cfg=cfg,                        # RunConfig（由 orchestration.cli.build_config() 建立）
)
trader.run()  # DB 寫入、Telegram、heartbeat、KPI 更新全由引擎處理
```

`sink`（DB 寫入）、`notifier`（Telegram）、`order_adapter`（下單）都可以在建構子明確覆蓋成自己的實作，或傳 `None` 完全關閉；`cfg.no_db=True` 時三者皆預設關閉，不 import `db`/`brokers`/`notifications` 任何一個套件——這是 librae 能被單獨當函式庫使用、不必連帶裝 TimescaleDB/交易所 SDK 的關鍵設計。

#### 模式對比

| | 回測 (backtest) | 模擬 (sim) | 實盤 (live) |
|---|---|---|---|
| 資料來源 | 歷史 OHLCV | 即時 OHLCV（polling） | 即時 OHLCV |
| 執行器 | `core.make_fill()` | `LiveExecutor(simulation=True)` | `LiveExecutor(simulation=False)` |
| 下單 | 模擬成交 | 模擬成交 + Telegram 通知 | 真實下單 |

### 核心類型

#### Strategy 層

| 類型 | 說明 |
|------|------|
| `BaseStrategy` | 抽象基類，實作 `on_bar(ctx) -> list[Action]` |
| `Context` | 不可變快照：ts, symbol, symbols, bar, bars, positions, cash, period_index |
| `Action` | 策略意圖：`type` = long / short / close / hold |
| `Position` | 凍結持倉（給策略看）：symbol, side, entry_price, quantity, unrealized_pnl |
| `PositionState` | 可變持倉（引擎內部）：追蹤 periods_held, entry_commission, entry_slippage, entry_tax, total_entry_cost |

#### Execution 層

| 類型 | 說明 |
|------|------|
| `Fill` | 成交回報：price, quantity, commission, slippage, tax |
| `TradeResult` | 完成交易：entry/exit 全資訊 + PnL + periods_held |
| `TradePnL` | PnL 拆解：gross_pnl, net_pnl, commission, slippage, tax |
| `CostModel` | 成本模型（frozen）：multiplier, commission_rate, slippage_ticks, tick_size, tax, long/short_margin_rate, impact_coef（成交量衝擊係數，預設 0 關閉）, maintenance_margin_rate（維持保證金率，預設 0 關閉強平模擬） |

#### Output 層

| 類型 | 說明 |
|------|------|
| `BacktestOutput` | 頂層容器（frozen）：run_metadata + equity_curve + trades + metrics |
| `RunMetadata` | run_id, strategy, symbol, timeframe, start/end/run timestamps |
| `StrategyMetrics` | 績效指標：total_return, sharpe, sortino, calmar, max_drawdown, win_rate... |
| `OrderEventRecord` | 部位生命週期事件（open/add/reduce/close）|
| `EquityCurvePoint` | 單點：ts, equity, period_return, drawdown, benchmark_equity |

#### 共用函數

| 函數 | 說明 |
|------|------|
| `make_fill(action, price, cash, cost_model)` | 模擬成交（backtest 直接用） |
| `process_actions(actions, ...)` | 共用 action 迴圈（backtest + live 共用） |
| `close_position(pos, exit_price, cost_model)` | 平倉 PnL + proceeds |
| `liquidate_all(positions, bars, ts, ...)` | 全平倉（end-of-run / 最大回撤熔斷共用） |
| `scale_into_position(pos, fill, cost_model)` | 同方向加碼（weighted avg entry） |
| `reduce_position(pos, quantity, exit_price, cost_model)` | 部分平倉 |
| `calc_trade_pnl(...)` | 單筆交易 PnL 拆解 |
| `compute_all(equity_values, timestamps, trade_pnls, ...)` | 績效計算（QuantStats adapter） |
| `direction(side)` | `"long"` → +1.0, `"short"` → -1.0 |

### 設計決策

- **Primitive signature**: `compute_all()` 接受 `Sequence[float]` / `Sequence[datetime]`，不依賴 `BacktestResult`，讓 live 引擎也能直接呼叫。
- **Lazy import**: `quantstats` 在 `compute_all()` 內延遲載入，`import librae` 保持 <1s；`db`/`brokers`/`notifications` 同理，見上方「依賴方向」。
- **PositionState in core**: backtest 和 live 共用同一個可變持倉型別，追蹤 `total_entry_cost` 避免 scaling 時浮點數漂移。
- **Pre-computed bars**: `_precompute_bars()` 一次性將 DataFrame 轉為 dict-of-dicts，避免 hot loop 中每 bar 呼叫 `to_dict()`。
- **Frozen dataclasses**: `BacktestOutput`, `StrategyMetrics`, `OrderEventRecord`, `CostModel` 等皆為 frozen，確保不可變。
- **Margin rate 統一公式**: `margin_rate` = 從可用現金流出的比例 / notional。開倉 `cash -= notional * margin_rate + costs`，平倉 `proceeds = notional * margin_rate + gross_pnl - exit_costs`，equity `mtm += unrealized + notional * margin_rate`。一個公式覆蓋現貨（1.0）、美股做空（0.5, Reg T）、台股融券（0.9）、期貨（0.067）。使用者可透過 `cost_overrides` 覆蓋預設值。

### Config API

> 完整設定檔清單（環境變數、YAML 範例、CLI 參數表）見 [根目錄 README「設定檔總覽」](../README.md#設定檔總覽)。以下僅說明引擎內部的程式碼調用方式。

#### MarketConfig（市場成本）

預設來源：`librae/config/markets.yaml`（程式啟動時讀取）；也可以完全不碰這個檔案，自己組一份 registry 傳進去（外部套件使用 librae 時常用）：

```python
from librae.config.market_config import get_market
from librae.core.cost_model import CostModel

market = get_market("crypto")            # → MarketConfig（讀 librae 內建 markets.yaml）
cost_model = CostModel.from_market(market)

# 或者：完全不依賴 librae 內建 markets.yaml，自己註冊市場
my_markets = {"my_market": MarketConfig(name="my_market", commission_rate=0.001, ...)}
market = get_market("my_market", markets=my_markets)
cost_model = CostModel.from_config(cfg, markets=my_markets)
```

#### TelegramAdapter（通知）

來源：行為設定從 `strategies/*/config.yaml` 的 `telegram:` 區塊，secrets 從環境變數。`librae` 本身不依賴這個套件——`LiveTrader` 只在沒被明確覆蓋、且 `cfg.no_db=False` 時才會 lazy import 它建立預設實作。

```python
from notifications.config import TelegramConfig
from notifications.telegram import TelegramAdapter, TelegramCredentials

config = TelegramConfig.from_dict(yaml_dict.get("telegram", {}))
creds = TelegramCredentials.from_env("TELEGRAM")
adapter = TelegramAdapter(config=config, credentials=creds)
```

`TelegramAdapter` 方法與對應 flag（定義在 `notifications/config.py`）：

| 方法 | Flag | 預設 |
|------|------|------|
| `send_signal()` | `notifications.signal` | `True` |
| `send_startup()` / `send_shutdown()` | `notifications.startup` | `True` |
| `send_alert()` | `notifications.error` | `True` |
| `send_status()` | `notifications.status.enabled` | `False` |

#### parse_with_config（CLI + YAML 合併）

策略 `run.py` 用這組函數處理 CLI 參數和 config.yaml 合併。
`telegram` 等巢狀區塊自動分離為 dict，不經過 argparse。

```python
from orchestration.cli import base_parser, parse_with_config, setup_logging

p = base_parser("My strategy")
args = parse_with_config(p, config_path=Path(__file__).parent / "config.yaml")
# args.mode, args.dry_run      ← runtime flags (argparse)
# args.strategy                ← dict from config.yaml strategy: block
# args.telegram                ← dict from config.yaml telegram: block
```

## 資料流

三個獨立的資料流各自畫一個子圖（同名節點在不同子圖裡代表同一張表，只是拆開避免線交錯，實際 schema 以下面「資料庫設計規範」為準）：

```mermaid
flowchart TD
    subgraph read["讀取：DB-first + 缺口補值"]
        get_ohlcv["get_ohlcv()"] -- "DB 有資料" --> direct1["直接回傳"]
        get_ohlcv -- "DB 缺口" --> apifill["API 補齊 → 寫回 DB"]
        get_ohlcv -- "DB 不可用" --> fallback["API fallback（不寫入）"]
        apifill --> r_ohlcv[("ohlcv")]
        apifill --> r_ohlcv_cov[("ohlcv_coverage_ranges")]

        get_factor["get_factor()"] -- "DB 有資料" --> direct2["直接回傳"]
        get_factor -- "DB 缺口" --> factorfill["fetcher 補齊 → 寫回 DB"]
        factorfill --> r_factors[("external_factors")]
        factorfill --> r_factor_cov[("external_factor_coverage_ranges")]
    end

    subgraph backtest["回測結果寫入"]
        save_signal["save_signal_results()"] --> b_signal_events[("signal_events")]
        save_signal --> b_ohlcv[("ohlcv")]

        save_strategy["save_strategy_results()"] --> b_backtest_runs[("backtest_runs")]
        save_strategy --> b_equity_curve[("equity_curve")]
        save_strategy --> b_trade_events[("trade_events")]
        save_strategy --> b_strategy_perf[("strategy_performance")]
        save_strategy --> b_signal_events
        save_strategy --> b_ohlcv
    end

    subgraph live["sim/live 即時寫入"]
        callbacks["LiveTrader callbacks"] -- on_order_event --> l_trade_events[("trade_events")]
        callbacks -- on_signal_outcome --> l_signal_events[("signal_events")]
        callbacks -- on_bar --> l_equity_curve[("equity_curve")]
        callbacks -- on_ohlcv --> l_ohlcv[("ohlcv")]
    end
```

## 資料庫設計規範

### 資料表命名規則

| 類型 | 規則 | 範例 |
|---|---|---|
| 離散事件/紀錄表（每列代表一次獨立發生的事件或紀錄） | 複數 | `backtest_runs`, `trade_events`, `signal_events`, `ohlcv_coverage_ranges` |
| 代表連續時序整體的領域慣用詞（每列是整體序列的一個點，但表名指稱的是序列本身） | 維持領域單數慣用詞 | `equity_curve`, `ohlcv` |

### 時間戳記命名規則

**`ts` 只保留給 hypertable 的時間維度欄位**（`ohlcv`/`equity_curve`/`trade_events`/`signal_events` 的分區鍵，代表「這一列發生的時間」）。
**所有其他時間點中繼資料一律用 `_at` 後綴**，即使是作為查詢範圍過濾參數（例如 `load_ohlcv(started_at=..., ended_at=...)`）也一致套用，避免同一個字根在不同函式簽章裡時而叫 `ts` 時而叫別的名字。

| 欄位 | 意義 | 出現位置 |
|---|---|---|
| `started_at` | run 的資料區間起點 | `backtest_runs`, `RunMetadata`, `load_ohlcv()` 查詢參數 |
| `ended_at` | run 的資料區間終點 | 同上 |
| `run_at` | run 被執行/建立的時間 | `backtest_runs`, `RunMetadata` |
| `entry_at` | 部位進場時間 | `trade_events`, `Position`, `PositionState`, `TradeResult`, `OrderEvent`, `OrderEventRecord` |
| `exit_at` | 交易出場時間 | `TradeResult` |
| `last_heartbeat_at` | 執行程序最後一次回報存活的時間 | `backtest_runs` |
| `range_started_at` | 快取覆蓋區間起點 | `ohlcv_coverage_ranges` |
| `range_ended_at` | 快取覆蓋區間終點 | `ohlcv_coverage_ranges` |

### 現行 9 張表一覽

| 表名 | 用途 | PK / FK | Hypertable |
|---|---|---|---|
| `backtest_runs` | Run 中樞，1 row / run | PK `run_id` | 否 |
| `equity_curve` | 每 bar 淨值 | FK `run_id` → `backtest_runs` CASCADE | 是（`ts`） |
| `trade_events` | 部位生命週期事件（open/add/reduce/close） | FK `run_id`（nullable） | 是（`ts`） |
| `strategy_performance` | 聚合 KPI，1 row / run | PK+FK `run_id` → `backtest_runs` CASCADE | 否 |
| `ohlcv` | 共用市場資料（`get_ohlcv()` cache） | 無 FK | 是（`ts`） |
| `signal_events` | 訊號品質監控（策略原始訊號，非成交紀錄） | FK `run_id`（nullable） | 是（`ts`） |
| `ohlcv_coverage_ranges` | `get_ohlcv()` 快取覆蓋區間追蹤（每列一個 range） | 無 FK | 否 |
| `external_factors` | 第三方因子資料（funding rate、open interest...），一致 schema 的 long table，新資料源不用 migration，`get_factor()` 自動寫入 | 無 FK（unique index: ts+symbol+factor_name+source+instrument_type） | 是（`ts`） |
| `external_factor_coverage_ranges` | `get_factor()` 快取覆蓋區間追蹤，跟 `ohlcv_coverage_ranges` 同一套機制 | 無 FK | 否 |

### 數量歧義處理原則

同一筆紀錄若同時存在「本次成交量」與「事件後剩餘部位量」，禁止用 `quantity` 泛稱兩者 —— 名稱本身要能區分語意。統一用：

- `fill_quantity` — 本次事件的成交量
- `remaining_quantity` — 事件後剩餘部位量

**只在同時持有兩者的類別上做這個區分**（`trade_events` 表、`OrderEvent`、`OrderEventRecord`）。單一數量欄位的類別（`Position.quantity`、`PositionState.quantity`、`Fill.quantity`、`Action.quantity`、`TradeResult.quantity`）維持 `quantity` 不變 —— 它們本身沒有歧義，不需要比照修改。

### 純量計數不可用複數

代表「持有了幾根 bar」的整數計數統一用 `periods_held`，不用複數形式（複數容易誤讀成列表）。套用在所有代表這個概念的欄位/屬性上：`trade_events.periods_held`、`Position.periods_held`、`PositionState.periods_held`、`TradeResult.periods_held`、`OrderEvent.periods_held`、`OrderEventRecord.periods_held`。

### 報酬率命名

`period_return` / `benchmark_period_return`：每個 bar 的報酬率，不綁定特定頻率字眼（不用 `1d` 這類字根）—— `timeframe` 可以是 1h/4h/1d 任何頻率，命名不該暗示固定為日頻。

## Python 函數命名規範

### `db/timescale_writer.py`（五類動詞，寫在該檔案的 module docstring）

```
write_*   — 單表 INSERT/UPSERT（可包含型別/時區正規化），整列寫入
update_*  — 單表局部 UPDATE，只更新既有列的部分欄位
merge_*   — 單表 read-modify-write 整併邏輯（例如區間合併），超出單純 UPSERT 範圍
save_*    — 多表交易性協調器；可能從更廣的輸入中萃取/轉換資料
refresh_* — 從其他表重新計算衍生/聚合資料並 upsert 結果
```

判斷準則：**單表 vs 多表**決定 `write_`/`save_` 二選一；**整列寫入 vs 局部更新既有列**決定 `write_`/`update_`；**是否需要先讀取既有資料才能決定寫入內容**（而非單純 UPSERT）用 `merge_`；**是否從其他表重新聚合**用 `refresh_`。

例：`save_backtest_output`（一次寫 5 張表，多表協調器）、`write_trade_event`（單表整列寫入）、`update_heartbeat`（單表局部更新一個欄位）、`merge_ohlcv_coverage_ranges`（要先讀既有區間才能決定合併結果）、`refresh_performance`（從 `equity_curve` + `trade_events` 重新算 KPI 寫回 `strategy_performance`）。

**重複資料衝突處理**：`write_ohlcv()`/`write_external_factor()` 的 SQL 都是 `ON CONFLICT (...) DO NOTHING`——同一個主鍵（ts + symbol + timeframe/factor_name + data_source/source + instrument_type）已存在時，新抓到的值直接丟棄，DB 裡舊值不變（保留最先寫入的，不是保留最新的）。這是刻意的，跟 `us_fundamentals.py`'s `_first_disclosure()` 保留最早申報值、只警告不覆蓋是同一套 point-in-time 正確性哲學：回測要重現「當時看到的數字」，不能讓資料源事後的訂正悄悄改寫過去某個時間點的快照。

### `db/timescale_reader.py`（三類動詞，寫在該檔案的 module docstring）

```
get_*    — 單一純量 / 小型物件查詢（id、dict、list of tuples）
load_*   — 回傳 DataFrame 的批次查詢，供分析/dashboard 使用
derive_* — 從既有資料算出不同形狀的結果；不是原始表的直接讀取
```

例：`get_run_by_config_hash`（回傳 dict）、`load_trade_events`（回傳 DataFrame）、`derive_trade_signals`（從 `trade_events`「反推」出進出場訊號序列，**不是**在讀 `signal_events` 表 —— 這兩者容易混淆，命名刻意用 `derive_` 而非 `load_` 來提醒呼叫端這是衍生資料，不是原始訊號）。

## VM 部署與策略管理

VM 上完全不放程式碼，只跑 `deploy/` 目錄同一份 `docker-compose.yml`。

### 從一台全新的 VM 開始

以下三件事要先做完，才能進到下面「部署」的步驟——雲端服務商 GUI 操作因人而異，這裡只列出結果要滿足什麼條件：

1. **SSH 能連進去**：把本機的公鑰（`~/.ssh/id_ed25519.pub` 或等效檔案）加進 VM 的 metadata/authorized_keys。雲端主控台的「貼公鑰」欄位不一定可靠（貼了存了，實際卻沒生效——踩過這個坑，見 `docs/learnings/ERRORS.md`），能用 CLI 寫入 instance metadata 就優先用 CLI，寫完務必實際 `ssh <user>@<vm-ip>` 驗證一次，不要只看主控台顯示「已儲存」。之後一律用這個使用者連線（例如 `jason`）——GCP 主控台的 SSH 按鈕、或省略使用者名稱的 `gcloud compute ssh`，走的是你的 Google 身分（OS Login），會自動帶出另一個帳號（例如 `jasonpanbackup`），看不到 `quant-deploy` 底下的東西。
2. **固定的對外 IP**：預設配發的外部 IP 通常是動態的，重開機會換掉——升級成靜態 IP（雲端主控台的網路設定裡通常叫「保留靜態位址」/"Reserve static address"）。這個 IP 之後會用在：Binance API 白名單、`SHIOAJI_CA_PATH` 所在機器的識別。
3. **裝好 Docker**：`ssh` 進去後 `apt install -y docker.io docker-compose-plugin`（`cloud_deploy.sh`/`trade.sh` 都假設這兩個已經裝好，不會幫你裝）。

**SSH 防火牆**：SSH（`tcp:22`）的來源限制在 IAP（Identity-Aware Proxy）的固定 IP 段，不對整個網際網路開放：

```bash
gcloud compute firewall-rules update default-allow-ssh --source-ranges=35.235.240.0/20
```

平常用 Tailscale mesh IP 的 `ssh <user>@<tailscale-ip>` 不受影響（Tailscale 走額外的虛擬網路介面，跟這條防火牆規則管的實體網卡是兩回事）；Tailscale 連不上時，`gcloud compute ssh <instance> --zone=<zone> --tunnel-through-iap` 走 IAP 通道當緊急備援。直接對公網 IP 的 SSH（沒裝 Tailscale、也不是用 `gcloud` 的裝置）連不進去，這是預期行為。Grafana 的 `librae-grafana`（port 3000）是完全獨立的另一條防火牆規則，維持公開 + 密碼登入，不受這條 SSH 規則影響。

```bash
# 1. 一次性：在 VM 上裝 Tailscale，取得私有 mesh IP
./deploy/bootstrap_tailscale.sh <user>@<vm-host>

# 2. 拿到上一步印出的 tailscale IP 後，本機 .env 設定 TSDB_BIND=<tailscale-ip>
#    （預設 127.0.0.1 只綁 loopback，Tailscale 連不到，DB 等於連不上）

# 3. 部署：把 deploy/ + Grafana provisioning + .env 同步過去，啟動 timescaledb + grafana
./deploy/cloud_deploy.sh <user>@<tailscale-ip>
```

`cloud_deploy.sh` 只 rsync `deploy/` 和 `app/grafana/provisioning/`，VM 上除了這兩個資料夾和 `.env` 之外沒有任何 repo 內容（若要 live 下單，另外還有手動建立、不受這支腳本管理的 `.env.secrets`，見下）——之後要更新 dashboard 或 schema，重跑一次這支腳本即可，不需要 SSH 上去手動改。

本機接上遠端 DB：
```bash
export TIMESCALE_DSN="postgresql://quant:<密碼>@<tailscale-ip>:5432/quant"
psql "$TIMESCALE_DSN" -c "SELECT 1"   # 驗證連線
```

用 GUI 工具查資料（例如 VS Code 的 PostgreSQL extension、TablePlus）也是接同一組連線資訊：host 填 `<tailscale-ip>`、port `5432`、user/password/db 跟 `.env` 的 `POSTGRES_PASSWORD` 一致——走 Tailscale mesh，不需要另外開防火牆port。

Grafana 的 port mapping（`3000:3000`）沒有限制 bind IP，容器內部是對所有介面開放、只靠帳號密碼擋（`GF_AUTH_ANONYMOUS_ENABLED=false`），跟 DB 刻意限制在 Tailscale 不同。外部是否連得到還要看 VM 的雲端防火牆/security group 有沒有開放 3000 對外——用一台沒裝 Tailscale 的裝置打 `http://<vm-公網ip>:3000` 驗證實際曝露範圍。

### 讓策略常駐 VM（sim/live 容器，一樣不用 clone repo）

`trade.sh` 平常在本機用時會直接 `docker build` 整個 repo；要放到沒有 repo 的 VM 上跑，改成本機 build + push、VM 只 pull。

**這是 VM 上跑 `trade.sh` 的必要前置條件，不是可選優化**——VM 上沒有原始碼，`TRADE_IMAGE` 沒設的話 `trade.sh start` 會嘗試本地 `docker build`，但沒有 repo 可以 build，直接失敗。本機開發/測試不受影響（沒設 `TRADE_IMAGE` 就照舊本地 build），只有「要在 VM 上跑」這件事需要先做完下面幾步：

**0. 一次性：GitHub Container Registry 認證**（其他 registry 概念相同，跳過即可）：GitHub 網頁 Settings → Developer settings → Personal access tokens → Tokens (classic) 建一個新 token，勾 `write:packages`（會自動帶 `read:packages`）；本機用它登入一次：

```bash
docker login ghcr.io -u <github 帳號>   # 密碼欄貼 PAT，不要用 GitHub 密碼
```

之後憑證會存在本機 `~/.docker/config.json`，`build_push.sh` 都會沿用，不用每次重登。

```bash
# 1. 本機：.env 設 TRADE_IMAGE=ghcr.io/<github-user>/quant-trade，
#    build 一次、push 到 registry（之後只有策略程式碼改了才需要重跑）
./deploy/build_push.sh

# 2. VM 上（deploy/ 已經被 cloud_deploy.sh 同步過去，.env 也有 TRADE_IMAGE）：
# <strategy_name> = strategies/<name>/ 底下通過因子驗證、有 run.py 的 production 策略
# （目前沒有任何策略在此狀態——見 strategies/FACTOR_ANALYSIS.md）
cd deploy && ./trade.sh start <strategy_name> sim 60    # 訊號推播，不下真單
cd deploy && ./trade.sh start <strategy_name> live 60   # 真實下單（見下方風險說明）
```

`trade.sh start` 看到 `.env` 有 `TRADE_IMAGE` 就會改成 `docker pull` 而不是本地 build——VM 上完全不需要原始碼。

`live` 模式需要的密鑰放在獨立的 `.env.secrets`（範本 `.env.secrets.example`：`BINANCE_API_KEY`/`BINANCE_API_SECRET` 或 `SHIOAJI_*`，看這台 VM 要跑哪個市場），**不是**會被 `cloud_deploy.sh` 整份覆蓋的 `.env`，只能直接在 VM 上手動建立：

```bash
# 只在真的要下單那台 VM 上做一次（Binance 只需要前兩步）：
ssh <user>@<vm-ip> "mkdir -p ~/quant-deploy/.secrets && chmod 700 ~/quant-deploy/.secrets"

# Shioaji 才需要：把本機的 CA 憑證傳過去，路徑跟 SHIOAJI_CA_PATH 對齊
scp ./.secrets/Sinopac.pfx <user>@<vm-ip>:~/quant-deploy/.secrets/Sinopac.pfx
ssh <user>@<vm-ip> "chmod 600 ~/quant-deploy/.secrets/Sinopac.pfx"

# 兩者都要：建立 .env.secrets，填入真正有交易權限的 key（不要用本機那把唯讀 key）
ssh <user>@<vm-ip>
cd quant-deploy && cp .env.secrets.example .env.secrets && $EDITOR .env.secrets
chmod 600 .env.secrets
```

這樣交易 key 只存在這一台機器：本機不會有它，之後重跑 `cloud_deploy.sh` 更新 dashboard/schema 也不會不小心把 VM 上的 key 蓋成空值。`trade.sh start ... live` 會自動 source `.env` + `.env.secrets`，並依 `.env.secrets` 裡實際存在哪組 key 注入對應的環境變數；有 `SHIOAJI_CA_PATH` 且該檔案存在時，還會把整個 `.secrets/` 唯讀掛進容器。市場本身是策略 `symbol` 在 `librae/config/symbols.yaml` 自動解析出來的（見 README「策略開發流程」），`trade.sh` 不需要另外指定。

Binance key 在交易所後台申請時：VM 這把要開「交易」權限，並把 IP 白名單設成 VM 的**固定外部 IP**（`gcloud compute instances describe <instance> --format='value(networkInterfaces[0].accessConfigs[0].natIP)'` 查得到；不是 Tailscale 的 `100.x.x.x` mesh IP，交易所看到的是實際對外連線來源）。本機如果只是開發/回測，不用申請 key；真的要在本機手動測下單，另外開一把獨立、權限盡量低（唯讀或交易所的測試網/demo）的 key，不要跟 VM 那把共用。

Shioaji 一樣：VM 上放 full 權限 key + CA 憑證，本機日常開發只留一把「唯讀」權限的 key、不要放 CA（`ShioajiAdapter` 沒填 `SHIOAJI_CA_PATH` 會自動進 read-only，下單方法直接拋錯，不怕手滑打到真單 API）。CA 憑證上雲端這件事風險在於：VM 被入侵 = key 外洩；Tailscale 只降低「誰連得到這台 VM」的風險，不降低「VM 本身被攻破」的風險，這是兩回事——是刻意接受的風險換取自動化部署，不是沒考慮過。

#### `mode`（sim/live）vs `sandbox`（測試網/模擬環境）

兩個正交的開關，容易搞混：

- **`mode`**：策略要不要真的送單。`sim` 只本地記帳，從不呼叫 `place_order`；`live` 才會把成交鏡射成真實訂單送到 broker。
- **`sandbox`**（`.env.secrets` 的 `BINANCE_SANDBOX`/`SHIOAJI_SANDBOX`，跟 `api_key` 同一套 `CredentialConfig.from_env` 載入機制）：訂單送到哪個環境，跟策略邏輯無關。`false` 正式站；`true` 測試網/模擬帳戶（假錢）。

常用組合：

| mode | sandbox | 用途 |
|------|---------|------|
| sim | false（預設） | 日常開發：抓正式行情，不下單 |
| live | true | 上線前端到端演練：真送單邏輯，但送到模擬環境，驗證訊號→下單全流程不動真錢 |
| live | false | 正式上線交易 |

兩個 broker 的模擬環境不同，準備測試 key 的方式也不同：

- **Binance**：測試網是獨立站點，需要另外申請一把只認測試網的 key，跟正式 key 互不相通。
- **Shioaji**：模擬交易跟正式站共用同一組 key/CA，靠 `simulation` 參數切換，不用另申請 key。但 CA 啟用跟「key 有沒有交易權限」是兩關：sandbox 一樣要 CA 成功才能下單；Token 交易權限是永豐後台另開的，跟 CA/sandbox 無關——本機唯讀 key 就算 CA 啟用成功，送單仍會被擋（`401 Token doesn't have permission`）。要測 live+sandbox 的完整送單路徑，得用 VM 上那把有交易權限的 key。

常用管理指令：

| 指令 | 說明 |
|------|------|
| `./deploy/build_push.sh` | 本機 build + push trade image（策略程式碼改了才需要） |
| `./deploy/trade.sh start <strategy_name> sim 60` / `trade.sh stop <strategy_name> sim` | 啟停常駐 sim 容器（本機或 VM 上執行皆可） |
| `./deploy/trade.sh start <strategy_name> live 60` / `trade.sh stop <strategy_name> live` | 啟停常駐 live 容器（真實下單，crypto 限定） |
| `python scripts/check_heartbeat.py --loop` | 監控 sim/live 是否掛掉（`backtest_runs.last_heartbeat` 超過 3 × poll_seconds 沒更新就 Telegram 告警） |

## 維護規則

1. 新增/修改 table、column，或 `db/timescale_writer.py`、`db/timescale_reader.py` 裡的讀寫函數時，同步更新本文件對應章節。
2. 新增欄位如果碰到「這個名字算不算歧義」「該不該用 `_at`」等邊界判斷，對照上面「數量歧義處理原則」「時間戳記命名規則」的準則，而不是逐案自行決定。
3. 若命名規則本身要改變（而非單純新增條目），在 `docs/decisions/` 開一份新的 decision 記錄改動原因，本檔案改完後只反映最終現況，不保留舊規則的說明。
