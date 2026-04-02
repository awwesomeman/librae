# quant-strategy-lab Implementation Plan

> 狀態：in-progress
> 範圍：全系統（engine, strategy, sim, grafana）
> 建立日期：2026-03-06
> 最後更新：2026-03-31
> 依據：[2026-03-06 核心決策](../decisions/2026-03-06-core-tooling-and-schema.md)
> 進度：Phase 3 sim mode 運行中；trendpullback_m5 已完成回測並正在監控

---

## 1) Goal Alignment

### End goal
Signal subscription and live auto trading platform for futures, crypto, pair trading, stock selection.

### 兩大目標

| # | 目標 | 說明 | 對應 Phase |
|---|------|------|-----------|
| **Goal 1** | **Signal Subscription** | 即時偵測信號 → 推播通知給訂閱者 | Phase 3 |
| **Goal 2** | **Live Auto Trading** | 基於信號自動下單 → 持倉管理 → 風控 | Phase 4 |

Goal 1 是 Goal 2 的前提 — LiveExecutor `simulation=True` 即為信號推播，`simulation=False` 即為自動交易。同一套程式碼，不同模式。

### Current phase goal
Phase 2：端到端回測 pipeline 跑通（共同基礎，兩個目標都需要）。

---

## 2) Architecture

### 三層解耦

```
ETL (pipeline/)       → df (MultiIndex + 信號欄位)
Strategy (strategies/) → on_bar(ctx) → Action[]
Engine (librae/)       → Executor.execute(action) → Fill → BacktestResult
```

### 回測 vs 實盤（共用 Strategy）

```
                回測                              實盤
                ────                              ────
Data:      fetcher → DataFrame              broker.stream_bars()
Strategy:  strategy.on_bar(ctx) → Action[]  strategy.on_bar(ctx) → Action[]  ← 同一份
Executor:  core.make_fill(CostModel)        LiveExecutor(simulation=True/False)
Close:     core.close_position(PositionState, price, CostModel) → (TradePnL, proceeds)  ← 共用
```

### 專案結構

```
quant-strategy-lab/
├── librae/                  ← 回測引擎框架（純引擎，可獨立抽出）
│   ├── core/               # backtest + live 共用 domain model（純計算，無 I/O）
│   │   ├── strategy.py     # BaseStrategy, Action, Context, Position, PositionState, Fill
│   │   ├── executor.py     # make_fill, calc_trade_pnl, close_position, TradeResult, TradePnL
│   │   ├── cost_model.py   # CostModel（multiplier 統一現貨/期貨, commission+tax 分離）
│   │   ├── metrics.py      # compute_all（QuantStats adapter）
│   │   └── utils.py        # generate_run_id, infer_timeframe, to_ccxt, to_canonical
│   ├── backtest/           # 回測 runtime
│   │   ├── engine.py       # Backtest + build_output
│   │   ├── schema.py       # BacktestOutput, RunMetadata, StrategyMetrics, TradeRecord
│   │   └── persistence.py  # save_output / load_output（JSON + CSV + Parquet）
│   ├── live/               # live/sim runtime
│   │   ├── engine.py       # LiveTrader（polling loop）
│   │   ├── executor.py     # LiveExecutor（sim 通知 / live 下單）
│   │   └── wiring.py       # build_live_trader（convenience factory）
│   ├── config/             # markets.yaml（市場 / 標的設定）
│   ├── notifications/      # Telegram 推播
│   └── cli.py              # 共用 CLI parser + config YAML 載入
│
├── data/                    ← 專案層：資料取得（librae 不依賴）
│   └── binance.py          # Binance OHLCV fetcher + cache + resample
│
├── strategies/              ← 策略實作（BaseStrategy 子類 + 純信號函數）
│   ├── trendpullback/       # H1 策略：D1 趨勢 + H1 回調
│   └── trendpullback_m5/   # M5 策略：M30 趨勢 + M5 回調
│
├── pipeline/                ← 資料取得 + ETL
├── brokers/                 ← 券商 adapter（三層分離：MarketData / Order / Account）
├── db/                      ← TimescaleDB 讀寫
├── app/                     ← UI（Streamlit + Grafana）
├── deploy/                  ← docker-compose, SQL
├── tests/                   ← 按模組分目錄（engine/, strategies/, pipeline/, ...）
└── docs/                    ← 文件 + docs/decisions/（ADR）
```

### Tech Stack

| Area | Tool | 說明 |
|------|------|------|
| 回測引擎 | `librae/backtest/` | Backtest class + build_output()，Strategy Protocol, core/ 共用 domain model |
| Live/Sim | `librae/live/` | LiveTrader + build_live_trader()：DB callbacks + Telegram + heartbeat |
| 共用計算 | `librae/core/` | calc_trade_pnl, close_position, compute_all, direction, PositionState |
| CLI 共用 | `librae/cli.py` | base_parser + config YAML 載入 + setup_logging |
| 成本模型 | `librae/core/cost_model.py` | multiplier 統一現貨/期貨, CostModel.from_market() |
| 績效指標 | QuantStats + 客製指標 | `librae/core/metrics.py` compute_all（primitive types） |
| 信號條件 | `strategies/trendpullback/utils.py` | compute_entry/exit_conditions（純布林 Series） |
| 資料格式 | MultiIndex DataFrame (symbol, datetime) | 單資產是特例，多資產統一 |
| 研究/參數掃描 | vectorbt（開源版） | Phase 5 |
| Market Config | `librae/config/markets.yaml` | 兩層：MarketConfig + InstrumentConfig |
| 執行層 | CCXT / Shioaji | brokers/ 三層 adapter + CryptoAdapter |
| Time-series DB | TimescaleDB | 唯一資料源 |
| Dashboards | Streamlit + Grafana（單一 Strategy Dashboard，mode 篩選） | 統一在 app/ |
| Deployment | docker-compose + Tailscale | VPS 或 GCE |
| Testing | pytest 232 tests | 按模組分目錄（含 look-ahead bias + LiveTrader） |

### 設計原則

- **三層解耦**：ETL / Strategy / Engine 各做各的事
- **Strategy 不追蹤持倉**：看 `ctx.positions`（engine 擁有），用 `Action` 表達意圖
- **Engine 擁有所有狀態**：positions, bars_held, cash
- **Executor 可替換**：回測用 core.make_fill()，實盤用 LiveExecutor
- **同一份 Strategy 跑回測和實盤**：零修改
- **CostModel 內部實作**：使用者不需知道，Backtest 自動從 markets.yaml 建
- **Python coding-standards skill**：所有程式碼遵循 `~/.claude/skills/python/coding-standards/` 規範（命名慣例、型別標註、錯誤處理、不過度設計）

---

## 3) Phase 進度

```
Phase 0–1 ✅           Phase 2 ✅            Phase 3 ⏳            Phase 4              Phase 5
Foundation +      ──→  E2E Backtest    ──→  Signal Sub     ──→  Live Trading    ──→  Scale
Engine Refactor         Pipeline              (Goal 1 MVP)        (Goal 2 MVP)        (Multi-asset)
                        (共同基礎)             Crypto (BTC)        Binance testnet     + 訂閱平台
```

### Phase 0 — Foundation ✅

| 項目 | 狀態 |
|------|------|
| signal_engine pure function（trendpullback） | ✅ |
| Market Config 兩層架構（markets.yaml） | ✅ |
| TimescaleDB 完全取代 InfluxDB | ✅ |
| Grafana 統一 Strategy Dashboard（Python generator + mode 篩選） | ✅ |
| Streamlit（TimescaleDB 讀取） | ✅ |
| Scheduler（APScheduler，mode=sim） | ✅ |

### Phase 1 — 回測引擎重構 + 目錄重組 ✅

| 項目 | 狀態 |
|------|------|
| Backtest class（MultiIndex, 多資產 positions dict） | ✅ |
| BaseStrategy ABC + Context/Action/Position/Fill | ✅ |
| Executor Protocol + make_fill() | ✅ |
| CostModel（multiplier, commission+tax 分離, CostModel.zero()） | ✅ |
| metrics.py → QuantStats adapter | ✅ |
| trendpullback 拆為 compute_entry/exit_conditions（純函數） | ✅ |
| runners.py make_backtest_fn() factory | ✅ → 已刪除（D1, vectorbt 取代） |
| 目錄重組：librae/ + strategies/ + pipeline/ + brokers/ + monitoring/ | ✅ |
| tests 按模組分目錄 | ✅ |
| 206 tests passed | ✅ |
| **Engine Framework Refactor（refactor_librae.md）** | ✅ |
| ├─ Package 重組：core/ + backtest/ + live/ 三層分離 | ✅ |
| ├─ 共用計算：calc_trade_pnl, close_position, PositionState, compute_all | ✅ |
| ├─ API 簡化：Backtest.build_output(), add_benchmark(), infer_timeframe | ✅ |
| ├─ 命名統一：instrument→symbol, cm→cost_model, LiveTrader, save_output | ✅ |
| ├─ Bug fix：live multiplier, direction, missing tax | ✅ |
| ├─ 效能：_precompute_bars, _eval_equity single-pass, lazy quantstats | ✅ |
| ├─ 清理：刪 12 shim + BacktestExecutor + legacy adapter + data.py 搬出 | ✅ |
| └─ 232 tests passed | ✅ |

### Phase 2 — E2E Backtest Pipeline（共同基礎）✅（剩 ≥2 策略對比）

> 完成標準：至少 1 個策略能端到端跑回測，結果寫進 DB，Grafana 可看。

| 項目 | 狀態 |
|------|------|
| brokers/ 三層 adapter 架構（MarketData/Order/Account ABC） | ✅ |
| CredentialConfig + from_env() 統一 credential 管理 | ✅ |
| CryptoAdapter（CCXT wrapper）+ MarketHub dispatcher | ✅ |
| async context manager + connected 狀態追蹤 | ✅ |
| AdapterBundle + build_adapter_bundle() factory | ✅ |
| TrendPullback BaseStrategy 子類 | ✅ |
| `strategies/trendpullback/run.py` CLI（backtest + DB write） | ✅ |
| 端到端驗證：fetch → ETL → 策略 → 回測 → JSON → DB → 可讀回 | ✅ |
| Grafana 統一 Strategy Dashboard（mode 篩選 + 可摺疊 row + Trade Detail 修正） | ✅ |
| 補回 look-ahead bias 測試（信號穩定性 + D1 merge + 引擎時機，9 tests） | ✅ |
| ≥2 策略可比較（Backtest 板 run_id 對比） | ✅ trendpullback + trendpullback_m5 |

### Phase 3 — Sim Mode（Goal 1 MVP）— 首個市場: Crypto (BTC) ← 當前

> 完成標準：BTC TrendPullback 策略能即時偵測信號，寫入 DB，Grafana 可看，推送 Telegram。

| 項目 | 狀態 |
|------|------|
| CryptoAdapter hardening（drop_incomplete, since, length warning） | ✅ |
| Telegram retry + rate-limit + HTML escape | ✅ |
| DB schema dedup（equity_curve + strategy_signals unique index） | ✅ |
| write_signal() + write_run_metadata() + write_equity_point() + write_trade() + write_performance() | ✅ |
| Executor 重構（make_fill/size_position 共用函式） | ✅ |
| LiveTrader（polling + OHLCV cache + PositionState + equity/trade/ohlcv recording） | ✅ |
| LiveExecutor(simulation=True) + notify_exit | ✅ |
| prepare_signals() 共用 pipeline | ✅ |
| `--mode sim` CLI + on_bar/on_trade/on_ohlcv callbacks | ✅ |
| LiveTrader + LiveExecutor 單元測試（14 tests） | ✅ |
| Sim run 註冊 backtest_runs（mode=sim） | ✅ |
| Docker sim service（Dockerfile.sim + docker-compose） | ✅ |
| Telegram bot 建立 + 本地端到端驗證 | ✅ |
| 命名一致性 monitor→sim（CLI, Docker, Grafana, DB） | ✅ |
| KPI 即時更新（_refresh_performance on trade close） | ✅ |
| Grafana 三模式共用 Dashboard（backtest/sim/live 資料完整對齊） | ✅ |
| Heartbeat liveness tracking（Status panel, Online/Offline） | ✅ |
| sim 腳本化部署（sim_start.sh / sim_stop.sh，多策略多標的同時跑） | ✅ |
| 引擎 API 重構（build_output, build_live_trader, base_parser, config YAML） | ✅ |
| TrendPullback M5 策略（M30 趨勢 + M5 進場，信號測試用） | ✅ |
| 統一 logging（print → logger） | ✅ |
| **VPS 部署 + Grafana 端到端驗證** | ✅ |
| trendpullback_m5 回測完成 + sim 監控中 | ✅ 運行中 |
| **多資產同時 sim 驗證** | ⏳ |

### Phase 4 — Live Auto Trading（Goal 2 MVP）

> 完成標準：BTC TrendPullback 能在 Binance testnet 自動交易。

| 項目 | 說明 |
|------|------|
| Binance live adapter | 實作真實 API（繼承 MarketDataAdapter + OrderAdapter） |
| LiveExecutor(simulation=False) | 真下單 + fill 回報 |
| 風控層 | 最大持倉、單筆上限、日虧損上限 |
| Position monitor | 持倉追蹤 + PnL dashboard |
| Signal Accuracy Dashboard | 信號品質監控（hit rate, regime breakdown） |
| Alerting | 異常狀態（斷線、拒單、超時）→ Telegram 告警 |

### Phase 5 — Scale（多資產 + 訂閱平台）

| 項目 | 說明 |
|------|------|
| Shioaji live adapter | 台指期真實交易 |
| 多資產策略驗證 | 選股、配對交易、套利 |
| Strategy Orchestrator | 策略 >5 個時，改為單一 container 內多 thread/process 管理多個 LiveRunner，減少 container 數量（目前一策略一 container，YAGNI） |
| FastAPI + 訂閱系統 | 使用者管理、策略訂閱、JWT |
| 版本化策略發布 | 策略打包 + 發布流程 |
| Streamlit vectorbt 研究工具 | 參數掃描 + 互動呈現 |

---

## 4) Key Decisions

見 `docs/decisions/` 目錄：
- `2026-03-31-database-schema-review.md` — Database schema review
- `2026-03-30-tsdb-bind-configurable.md` — TimescaleDB port binding 環境變數化
- `2026-03-28-strategy-folder-convention.md` — 策略目錄慣例
- `2026-03-27-backtest-engine-refactor.md` — 統一回測引擎 + CostModel + QuantStats
- `2026-03-26-platform-architecture.md`
- `2026-03-26-market-adapter-architecture.md`
- `2026-03-26-performance-metrics-standard.md`
- `2026-03-26-backtest-performance-optimization.md`
- `2026-03-26-dashboard-data-scope.md`
- `2026-03-25-dashboard-architecture.md`
- `2026-03-06-core-tooling-and-schema.md`

---

## 5) Config 重構 ✅

已完成。`librae/config/markets.yaml` 現在只保留市場屬性 + 成本參數：
- `warmup_bars`, `max_hold_bars` → 已移至策略層
- `data_source`, `exchange` → 已移至 broker layer

---

## 6) 待辦：Dashboard 指標擴充

目前 Performance Overview 只有 6 個 KPI（Total Return, Max DD, Sharpe, Win Rate, Profit Factor, Trades）。
需要設計如何加入更多指標（例如 Active Period Return、年化報酬、Sortino、Calmar）同時保持版面整潔。

### 考量

- **KPI row 寬度有限**：目前 6 × w=4 = 24 剛好滿，再加就要換行或縮窄
- **不同模式需求不同**：backtest 有完整歷史可算年化，sim 可能只跑幾天、年化無意義
- **指標重要性分層**：核心 KPI（一眼要看到）vs 進階指標（展開才看）

### 可能方向

1. **分層展示**：核心 KPI 維持第一行，進階指標放可摺疊 row（類似 Live/Sim Only）
2. **動態切換**：Grafana variable 選擇指標集（簡潔 / 完整），切換 KPI row 顯示內容
3. **Tooltip 補充**：主 KPI 不變，hover 時顯示相關延伸指標（如 Total Return hover 顯示 CAGR）
4. **Table panel**：一個表格列出所有指標，取代多個 stat panel，省空間

等指標數量 >10 時再決定方案。

---

## 7) Future Considerations（已評估、刻意延後）

> 以下項目在 librae 重構時經過批判檢視，確認現階段不實作但未來機率不低。記錄延後原因與觸發條件，避免重複討論。
> 參考框架：NautilusTrader, Zipline, Lumibot, vn.py, Backtrader, QSTrader, Freqtrade

### F1: Order 型別（Limit/Stop Order 支援）

- **現狀**：Action → `make_fill()` → Fill，1:1 mapping（market order only）
- **未來需求**：真實實盤幾乎一定需要 limit order / stop-loss order
- **需要的改動**：新增 `Order` dataclass 介於 Action 和 Fill 之間、partial fill 處理、order state tracking（Submitted → Filled / Cancelled）
- **為何不現在做**：目前只有 sim mode，market order 夠用。加 Order 層會讓 backtest engine 也要配合改動，scope 膨脹
- **延後成本**：低。在 `make_fill` 前插入 `Order` 層是 additive change，不需要重寫現有程式碼
- **觸發條件**：Phase 4 LiveExecutor(simulation=False) 上線時

### F2: Live State Recovery（重啟恢復部位）

- **現狀**：LiveTrader 每次啟動從零開始（stateless restart），sim mode 合理
- **未來需求**：production live trading 的 crash recovery 是剛需（重啟後需恢復當前部位、已實現淨值）
- **需要的改動**：新增 `initial_state: LiveState | None` optional 參數，從 DB 讀取部位初始化
- **為何不現在做**：YAGNI — 真正的 state recovery 需要考慮 partial fill、pending orders、order book state，預留簡化接口反而誤導
- **延後成本**：低。加一個 optional 參數即可，不影響現有 API
- **觸發條件**：Phase 4 真金白銀交易上線時

### F3: Config Dataclass 自動化（from_dict → dacite / generic utility）

- **現狀**：`TelegramConfig.from_dict()` / `NotificationConfig.from_dict()` 手動逐欄位 mapping，搭配 `bool()` 和 `.get()` fallback
- **未來需求**：當策略設定變成多層巢狀（如 risk_params、execution_policy），手寫 `from_dict()` 會變繁瑣且容易遺漏新欄位
- **候選方案**：`dacite`（輕量 dict-to-dataclass）、`dataclasses.replace` + 自寫 generic factory、或最終引入 `pydantic`
- **為何不現在做**：目前只有 3 個 dataclass（StatusConfig、NotificationConfig、TelegramConfig），欄位少，手寫完全可控。引入 dacite/pydantic 增加依賴但收益不大
- **延後成本**：低。`from_dict()` 是 classmethod，替換為 dacite 是 drop-in replacement，不影響呼叫端
- **觸發條件**：config dataclass 數量 >5 或巢狀深度 >3 層

### F4: Strategy Runner 共用模板（消除 run.py 重複）

- **現狀**：`trendpullback/run.py` 和 `trendpullback_m5/run.py` 的 `run_backtest()` / `run_sim()` / `main()` 幾乎完全相同，差異僅策略類別和 logger name
- **未來需求**：策略數量增加後，每新增一個策略需要 copy-paste 整份 run.py，bug fix 需多處同步
- **需要的改動**：提取共用 runner template（如 `librae/runner.py`），策略只需提供 strategy class + feature_fn + config path
- **為何不現在做**：目前只有 2 個策略，Rule of Three 尚未達標。且每個策略的 `fetch_and_prepare` / `prepare_signals` 有細微差異，過早抽象可能限制彈性
- **延後成本**：低。runner 是 leaf code，不影響引擎 API
- **觸發條件**：策略數量 ≥3 且 run.py 重複率 >80%

### 已評估但不納入規劃的項目

| 項目 | 來源框架 | 不採納原因 |
|------|----------|-----------|
| Lifecycle Hooks（initialize, before_market_opens, on_abrupt_close） | Lumibot | 現有 `on_bar` 已覆蓋；crypto 24/7 無 session 概念；額外 hooks 是 YAGNI |
| Event Queue / Dispatcher | NautilusTrader, QSTrader | Python 單機回測效能殺手；直接 method call 已具事件驅動語意 |
| Multi-subscriber Observer（register_callback pattern） | vn.py | Constructor injection 已實現解耦；5 個 callback 在可控範圍；超過 7 個再考慮 |
| Event Sourcing Store | NautilusTrader | 過度設計；structured logging（Action → Order → Fill 三點 log）已足夠 |
| Pipeline API（黑盒特徵計算） | Zipline | 特徵計算在策略外部完成（`feature_fn`），不需要框架內建 Pipeline |

---

## 8) Refactor 門檻

觸發任 2~3 條才考慮大幅重構：
1. 策略 >10 且重複邏輯 >40%
2. runner 效能成瓶頸（tick level 百萬+ bars → 考慮 Numba JIT）
3. 需要 tick/orderbook 回測（考慮 NautilusTrader）
4. 訂閱者 >50 且 API 延遲成瓶頸
5. 資料源 >3 導致 broker adapter 不一致
