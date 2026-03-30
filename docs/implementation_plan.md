# quant-strategy-lab Implementation Plan

> Updated: 2026-03-30
> Architecture: Librae 回測引擎 + Strategy Protocol + Executor 分離
> Status: Phase 2 接近完成（剩 ≥2 策略對比），Grafana 整合 + look-ahead bias 測試已完成

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
Executor:  BacktestExecutor(CostModel)      LiveExecutor(broker, simulation)
                                             ├─ simulation=True → 訊號通知
                                             └─ simulation=False → 真下單
```

### 專案結構

```
quant-strategy-lab/
├── librae/                  ← 回測引擎套件（純引擎，可獨立抽出）
│   ├── engine.py            # Backtest class（MultiIndex, 多資產 positions dict）
│   ├── strategy.py          # BaseStrategy ABC, Context, Action, Position, Fill
│   ├── executor.py          # Executor Protocol, BacktestExecutor
│   ├── cost_model.py        # CostModel（multiplier 統一現貨/期貨, commission+tax 分離）
│   ├── metrics.py           # QuantStats adapter → StrategyMetrics
│   ├── schema.py            # BacktestOutput, TradeRecord, EquityCurvePoint
│   ├── persistence.py       # save/load JSON/Parquet
│   ├── runners.py           # walk-forward, stability, strict protocol
│   ├── config/              # markets.yaml, market_config.py（引擎內部配置）
│   └── schemas/             # canonical_schema.json
│
├── strategies/              ← 策略實作（BaseStrategy 子類 + 純信號函數）
│   └── trendpullback/       # signals.py (entry/exit conditions), btc.py, mxfr1.py
│
├── pipeline/                ← 資料取得 + ETL
│   ├── fetchers/            # binance_fetcher.py
│   └── features/            # core_features.py, transforms
│
├── brokers/                 ← 券商 adapter（三層分離：MarketData / Order / Account）
│   ├── base.py              # ABCs + CredentialConfig + _ConnectableMixin
│   ├── sim.py               # Sim adapters（paper/backtest 用）
│   ├── binance.py           # Binance venue adapters（目前繼承 Sim）
│   ├── shioaji.py           # Shioaji venue adapters（目前繼承 Sim）
│   ├── crypto_adapter.py    # CCXT-based sync adapter（fetch_ohlcv, place_order）
│   ├── market_hub.py        # Multi-market dispatcher
│   └── wiring.py            # AdapterBundle + build_adapter_bundle() factory
│
├── monitoring/              ← 訊號監控 + 排程 + 通知（已遷移，僅剩 __pycache__）
│
├── db/                      ← TimescaleDB 讀寫
├── app/                     ← UI（Streamlit + Grafana）
├── deploy/                  ← docker-compose, SQL
├── scripts/                 ← CLI 入口
├── tests/                   ← 按模組分目錄（engine/, strategies/, pipeline/, ...）
├── docs/                    ← 文件 + docs/decisions/（ADR）
└── data/                    ← cache（gitignore）
```

### Tech Stack

| Area | Tool | 說明 |
|------|------|------|
| 回測引擎 | `librae/` | Backtest class, Strategy Protocol, Executor 分離 |
| 成本模型 | `librae/cost_model.py` | multiplier 統一現貨/期貨, CostModel.from_instrument() |
| 績效指標 | QuantStats + 客製指標 | `librae/metrics.py` thin adapter |
| 信號條件 | `strategies/trendpullback/signals.py` | compute_entry/exit_conditions（純布林 Series） |
| 資料格式 | MultiIndex DataFrame (instrument, datetime) | 單資產是特例，多資產統一 |
| 研究/參數掃描 | vectorbt（開源版） | Phase 5 |
| Market Config | `librae/config/markets.yaml` | 兩層：MarketConfig + InstrumentConfig |
| 執行層 | CCXT / Shioaji | brokers/ 三層 adapter + CryptoAdapter |
| Time-series DB | TimescaleDB | 唯一資料源 |
| Dashboards | Streamlit + Grafana（單一 Strategy Dashboard，mode 篩選） | 統一在 app/ |
| Deployment | docker-compose + Tailscale | VPS 或 GCE |
| Testing | pytest 206 tests | 按模組分目錄（含 look-ahead bias） |

### 設計原則

- **三層解耦**：ETL / Strategy / Engine 各做各的事
- **Strategy 不追蹤持倉**：看 `ctx.positions`（engine 擁有），用 `Action` 表達意圖
- **Engine 擁有所有狀態**：positions, bars_held, cash
- **Executor 可替換**：回測用 BacktestExecutor，實盤用 LiveExecutor
- **同一份 Strategy 跑回測和實盤**：零修改
- **CostModel 內部實作**：使用者不需知道，Backtest 自動從 markets.yaml 建
- **Python coding-standards skill**：所有程式碼遵循 `~/.claude/skills/python/coding-standards/` 規範（命名慣例、型別標註、錯誤處理、不過度設計）

---

## 3) Phase 進度

```
Phase 0–1 ✅           Phase 2 ⏳            Phase 3              Phase 4              Phase 5
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
| Executor Protocol + BacktestExecutor | ✅ |
| CostModel（multiplier, commission+tax 分離, CostModel.zero()） | ✅ |
| metrics.py → QuantStats adapter | ✅ |
| trendpullback 拆為 compute_entry/exit_conditions（純函數） | ✅ |
| runners.py make_backtest_fn() factory | ✅ |
| 目錄重組：librae/ + strategies/ + pipeline/ + brokers/ + monitoring/ | ✅ |
| tests 按模組分目錄 | ✅ |
| 206 tests passed | ✅ |

### Phase 2 — E2E Backtest Pipeline（共同基礎）← 當前

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
| ≥2 策略可比較（Backtest 板 run_id 對比） | ⏳ |

### Phase 3 — Signal Subscription（Goal 1 MVP）— 首個市場: Crypto (BTC)

> 完成標準：BTC TrendPullback 策略能即時偵測信號並推送 Telegram。

| 項目 | 說明 |
|------|------|
| LiveRunner | while loop，每根 bar 結束時跑 strategy.on_bar()，偵測 Action |
| LiveExecutor(simulation=True) | 收到 Action 不下單，改推送通知 |
| Telegram 訊號推播 | Signal → formatted message → Telegram bot |
| Signal Dashboard | Grafana/Streamlit 顯示即時信號 + 歷史命中率 |
| 部署 | docker-compose 跑 LiveRunner + Scheduler |

已有基礎：CryptoAdapter (CCXT) 可直接 fetch_ohlcv，不需額外實作 live adapter。

### Phase 4 — Live Auto Trading（Goal 2 MVP）

> 完成標準：BTC TrendPullback 能在 Binance testnet 自動交易。

| 項目 | 說明 |
|------|------|
| Binance live adapter | 實作真實 API（繼承 MarketDataAdapter + OrderAdapter） |
| LiveExecutor(simulation=False) | 真下單 + fill 回報 |
| 風控層 | 最大持倉、單筆上限、日虧損上限 |
| Position monitor | 持倉追蹤 + PnL dashboard |
| Alerting | 異常狀態（斷線、拒單、超時）→ Telegram 告警 |

### Phase 5 — Scale（多資產 + 訂閱平台）

| 項目 | 說明 |
|------|------|
| Shioaji live adapter | 台指期真實交易 |
| 多資產策略驗證 | 選股、配對交易、套利 |
| FastAPI + 訂閱系統 | 使用者管理、策略訂閱、JWT |
| 版本化策略發布 | 策略打包 + 發布流程 |
| Streamlit vectorbt 研究工具 | 參數掃描 + 互動呈現 |

---

## 4) Key Decisions

見 `docs/decisions/` 目錄：
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

## 5) 待辦：Config 重構

目前 `librae/config/markets.yaml` 的 InstrumentConfig 混了不同關注點：

```yaml
BTC_USDT:
  # 市場屬性（固定）— 保留
  tick_size, tick_value, trade_unit, min_qty, qty_precision, margin_rate

  # 成本參數（回測/實盤共用）— 保留
  commission_rate, min_commission, transaction_tax, slippage_ticks

  # 不該在這裡 — 未來拆出
  warmup_bars, max_hold_bars  → 策略參數（在 Strategy 類別裡）
  data_source, exchange       → 券商 config（brokers/ 或環境變數）
```

不在目前 Phase 做，等策略和券商模組穩定後再拆。

---

## 6) Refactor 門檻

觸發任 2~3 條才考慮大幅重構：
1. 策略 >10 且重複邏輯 >40%
2. runner 效能成瓶頸（tick level 百萬+ bars → 考慮 Numba JIT）
3. 需要 tick/orderbook 回測（考慮 NautilusTrader）
4. 訂閱者 >50 且 API 延遲成瓶頸
5. 資料源 >3 導致 broker adapter 不一致
