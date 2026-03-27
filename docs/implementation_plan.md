# quant-strategy-lab Implementation Plan

> Updated: 2026-03-28
> Architecture: Librae 回測引擎 + Strategy Protocol + Executor 分離
> Status: Phase 1 完成（引擎重構 + 目錄重組），Phase 2 進行中

---

## 1) Goal Alignment

### End goal
Signal subscription platform for futures, crypto, pair trading, stock selection.

### Current phase goal
回測引擎架構穩定，能端到端跑策略回測 + 寫 DB + Grafana 顯示。

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
├── brokers/                 ← 券商 adapter（一個 broker 同時負責資料和下單）
│   ├── base.py              # BaseBroker protocol
│   ├── binance.py, shioaji.py, sim.py
│   └── ...
│
├── monitoring/              ← 訊號監控 + 排程 + 通知
│   ├── signal_monitor.py, scheduler.py
│   ├── telegram.py
│   └── profiles/            # monitor config JSON
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
| 研究/參數掃描 | vectorbt（開源版） | Phase 2 待建 |
| Market Config | `librae/config/markets.yaml` | 兩層：MarketConfig + InstrumentConfig |
| 執行層 | CCXT / ib_insync / Shioaji | 直接包裝，不經 Lumibot |
| Time-series DB | TimescaleDB | 唯一資料源 |
| Dashboards | Streamlit + Grafana | 統一在 app/ |
| Deployment | docker-compose + Tailscale | VPS 或 GCE |
| Testing | pytest 331 passed | 按模組分目錄 |

### 設計原則

- **三層解耦**：ETL / Strategy / Engine 各做各的事
- **Strategy 不追蹤持倉**：看 `ctx.positions`（engine 擁有），用 `Action` 表達意圖
- **Engine 擁有所有狀態**：positions, bars_held, cash
- **Executor 可替換**：回測用 BacktestExecutor，實盤用 LiveExecutor
- **同一份 Strategy 跑回測和實盤**：零修改
- **CostModel 內部實作**：使用者不需知道，Backtest 自動從 markets.yaml 建

---

## 3) Phase 進度

### Phase 0 — Foundation ✅

| 項目 | 狀態 |
|------|------|
| signal_engine pure function（trendpullback） | ✅ |
| Market Config 兩層架構（markets.yaml） | ✅ |
| TimescaleDB 完全取代 InfluxDB | ✅ |
| Grafana 三板（Python generator） | ✅ |
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
| 331 tests passed | ✅ |

### Phase 2 — 端到端串接 + 策略研究（當前）

| 項目 | 狀態 |
|------|------|
| TrendPullback BaseStrategy 子類 | ⏳ |
| run_backtest.py 新版（用 Backtest class + MultiIndex） | ⏳ |
| 端到端驗證：取資料 → ETL → 策略 → 回測 → DB → Grafana | ⏳ |
| 補回 look-ahead bias 測試（新 API） | ⏳ |
| Streamlit 改版為 vectorbt 研究工具 | ⏳ |
| 參數掃描結果 TimescaleDB 表 + 互動呈現 | ⏳ |
| ≥2 策略可比較（Backtest 板 run_id 對比） | ⏳ |

### Phase 3 — 實盤 + 通知

| 項目 | 狀態 |
|------|------|
| LiveExecutor（simulation 參數切換真下單/訊號通知） | ⏳ |
| LiveRunner（while loop，等新 bar） | ⏳ |
| Broker wrappers：CCXTBroker, ShioajiBroker（資料 + 下單統一） | ⏳ |
| Grafana 告警 → Telegram | ⏳ |
| Telegram 訊號推播 | ⏳ |

### Phase 4 — 多資產 + 訂閱

| 項目 | 狀態 |
|------|------|
| 多資產策略驗證（選股、套利） | ⏳ |
| 台指期 ShioajiBroker | ⏳ |
| 使用者/訂閱（PostgreSQL + JWT） | ⏳ |
| FastAPI skeleton | ⏳ |
| 版本化策略發布 | ⏳ |

---

## 4) Key Decisions

見 `docs/decisions/` 目錄：
- `2026-03-27-backtest-engine-refactor.md` — 統一回測引擎 + CostModel + QuantStats
- `2026-03-26-platform-architecture.md`
- `2026-03-26-market-adapter-architecture.md`
- `2026-03-26-performance-metrics-standard.md`
- `2026-03-26-backtest-performance-optimization.md`
- `2026-03-26-dashboard-data-scope.md`
- `2026-03-25-dashboard-architecture.md`

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
