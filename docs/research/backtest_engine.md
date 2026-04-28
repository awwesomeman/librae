# Librae 演進路線分析：NautilusTrader 遷移 vs Rust 底層重寫

> 分析日期：2026-04-09

## Context

Librae 是一個 ~3,664 LOC 的純 Python 量化回測/實盤框架，架構清晰（Strategy → Engine → Executor → Output），已支援 TimescaleDB、Telegram 通知、Shioaji（台灣期貨）與 CCXT（加密貨幣）。目前效能瓶頸不大（BTC H1 五年 ~50ms），但隨著策略複雜度提升（多標的、tick-level、大規模參數掃描），純 Python 的天花板會逐漸浮現。

本分析比較三條路線：
1. **遷移到 NautilusTrader**
2. **Librae 底層改寫為 Rust（PyO3 混合架構）**
3. **維持現狀 + 漸進式優化**

---

## 一、現行 Librae 架構摘要

| 層級 | LOC | 關鍵模組 | 特性 |
|------|-----|---------|------|
| Strategy | 118 | `core/strategy.py` | `on_bar(ctx) → list[Action]`，純決策邏輯 |
| Engine | 466+550 | `backtest/engine.py`, `live/engine.py` | Bar-by-bar 迴圈，next-bar execution |
| Executor | 521 | `core/executor.py` | 開倉/加碼/減碼/平倉、PnL 計算，backtest/live 共用 |
| Metrics | 200 | `core/metrics.py` | QuantStats wrapper |
| Data | 400 | `data/ohlcv.py` | DB-first + API fallback，Parquet 快取 |
| DB | 600+ | `db/timescale_*.py` | TimescaleDB 6 張 hypertable |
| Brokers | 250+ | `brokers/` | CCXT + Shioaji adapter |

**效能現況：**
- BTC H1 5年（~40k bars）→ ~20-50ms
- BTC 5m 1年（~100k bars）→ ~100-200ms
- 已有優化：`_precompute_bars()` dict-of-dicts、lazy import quantstats

---

## 二、方案 A：遷移到 NautilusTrader

### 架構概述
- Rust core（Data Engine / Execution Engine / Risk Engine / Cache）+ Python API（PyO3）
- 單線程確定性 kernel + tokio 非同步 I/O
- 128-bit 整數精度、奈秒級時間戳
- Actor Model + Message Bus（>1M msg/sec）
- Backtest / Live 100% 同一 binary

### 優勢

| 面向 | 說明 |
|------|------|
| **效能** | 10-100x 提升（100k-500k events/sec），tick-level 回測無壓力 |
| **回測-實盤一致性** | 同一 Rust binary 處理兩種模式，消除 divergence |
| **風控引擎** | 內建 Risk Engine，微秒級 pre-trade check |
| **資料管理** | Apache Parquet + DataFusion，5M rows/sec streaming，不怕 OOM |
| **多標的擴展** | 數百標的幾乎無額外開銷 |
| **Broker 生態** | Binance/Bybit/OKX/IBKR/Databento 等 10+ adapter |
| **社群與維護** | 2,500+ GitHub stars，40+ contributors，持續活躍 |

### 劣勢

| 面向 | 說明 | 影響程度 |
|------|------|---------|
| **無 Shioaji adapter** | 台灣市場需自寫 Gateway adapter（Rust/Cython 層級），工程量極大 | **致命** |
| **學習曲線** | Event-driven 心智模型 + 大量 boilerplate，預計需數週上手 | 高 |
| **API 不穩定** | Cython→Rust 遷移期間頻繁 breaking changes，尚未到 v2.0 | 高 |
| **喪失架構控制** | CostModel（台灣當沖稅、期貨保證金）需遷就框架抽象 | 中高 |
| **無內建 Dashboard** | 需自建視覺化，目前 Grafana 整合要重寫 | 中 |
| **LGPL-3.0 授權** | 修改 Nautilus 原始碼需 open source，商業化需評估 | 低-中 |
| **不支援向量化掃描** | 大規模參數最佳化仍需 VectorBT 等工具 | 低 |
| **Python 3.12+ 要求** | v1.221.0+ 強制要求 | 低 |

### 遷移成本估算

| 項目 | 工作量 |
|------|--------|
| 學習框架 + 重寫策略邏輯 | 2-3 週 |
| 遷移 CostModel / 台灣市場邏輯 | 1-2 週 |
| 開發 Shioaji adapter（如需台灣實盤） | **4-8 週**（需深入 Rust Gateway 介面） |
| 遷移 DB/Dashboard 整合 | 1-2 週 |
| 測試驗證 | 1-2 週 |
| **合計** | **9-17 週** |

---

## 三、方案 B：Librae 底層改寫為 Rust（PyO3 混合架構）

### 架構設計

```
librae_core (Rust, via PyO3 → Python extension module)
├── executor:  process_actions, make_fill, calc_trade_pnl     ← hot path
├── metrics:   Sharpe/Sortino/MDD/Calmar window 計算          ← CPU-bound
└── backtest:  bar-by-bar loop + equity eval                   ← hot loop

librae (Python, 維持不變)
├── strategy/  → on_bar() 使用者程式碼
├── config/cli → YAML + argparse
├── data/db    → I/O bound，Python 足矣
├── brokers/   → Shioaji/CCXT adapter
└── notifications/ → Telegram
```

### 優勢

| 面向 | 說明 |
|------|------|
| **保留全部控制權** | 架構、CostModel、台灣市場規則完全自主 |
| **Shioaji 無縫保留** | Broker adapter 不需改動 |
| **漸進式遷移** | 可逐模組替換，風險可控 |
| **效能提升** | Hot path 預計 5-10x 加速（executor + bar loop） |
| **策略介面不變** | `on_bar(ctx) → list[Action]` 保持不動，使用者零感知 |
| **DB/Grafana 不動** | TimescaleDB + Dashboard 整合完全保留 |
| **學習投資可複用** | Rust + PyO3 技能可用於未來其他專案 |

### 劣勢

| 面向 | 說明 | 影響程度 |
|------|------|---------|
| **Rust 學習曲線** | 需學習 Rust + PyO3 binding，初期開發速度慢 | 高 |
| **雙語言維護成本** | Python + Rust 混合 codebase，debug 複雜度上升 | 中高 |
| **自建一切** | Risk Engine、Order Book、tick-level 支援都要自己做 | 中高 |
| **效能天花板較低** | 比 Nautilus 全 Rust 架構慢（Python↔Rust 邊界有 overhead） | 中 |
| **跨平台編譯** | macOS/Linux wheel 需 CI 配置 maturin | 低-中 |
| **測試複雜度** | 需同時測 Rust 單元 + Python 整合 | 中 |

### 開發成本估算

| 項目 | 工作量 |
|------|--------|
| 學習 Rust + PyO3 基礎 | 2-3 週 |
| 重寫 executor.py → Rust | 2-3 週 |
| 重寫 backtest hot loop → Rust | 1-2 週 |
| 重寫 metrics 核心計算 → Rust | 1-2 週 |
| Python binding + 型別安全介面 | 1 週 |
| CI/CD + wheel 打包（maturin） | 0.5-1 週 |
| 測試驗證 + 效能 benchmark | 1-2 週 |
| **合計** | **8-14 週** |

---

## 四、方案 C：維持現狀 + 漸進式 Python 優化

### 可做的優化

| 優化 | 預估加速 | 工作量 |
|------|---------|--------|
| Numba JIT 熱迴圈 | 3-5x | 1-2 天 |
| numpy vectorized equity calc | 2-3x | 1 天 |
| multiprocessing 參數掃描 | N 核 ≈ Nx | 2-3 天 |
| Polars 取代 Pandas（data prep） | 2-5x on data loading | 1 週 |
| 預計算更多中間值 | 1.5-2x | 數小時 |

### 優勢
- **零遷移成本**，立即可做
- 保留所有現有整合
- 團隊不需學新語言

### 劣勢
- Python 效能天花板仍在（GIL、動態型別）
- Numba 對複雜物件支援有限
- 無法支撐 tick-level 高頻回測

---

## 五、綜合比較矩陣

| 維度 | NautilusTrader | Rust 重寫 | 維持現狀 |
|------|---------------|-----------|---------|
| **效能提升** | ★★★★★ (50-100x) | ★★★★ (5-10x) | ★★☆ (2-5x) |
| **台灣市場支援** | ★☆ (需自建 adapter) | ★★★★★ (原生保留) | ★★★★★ |
| **開發時間** | 9-17 週 | 8-14 週 | 1-2 週 |
| **維護複雜度** | ★★★ (框架升級風險) | ★★★ (雙語言) | ★★★★★ (單語言) |
| **架構控制** | ★★ (受框架約束) | ★★★★★ (完全自主) | ★★★★★ |
| **功能完整度** | ★★★★★ (Risk/OrderBook) | ★★★ (需自建) | ★★★ |
| **長期擴展性** | ★★★★★ | ★★★★ | ★★☆ |
| **學習價值** | ★★★ (框架知識) | ★★★★★ (Rust+PyO3) | ★ |

---

## 六、使用者意向

| 問題 | 回答 |
|------|------|
| 主力市場 | **混合（台灣+加密貨幣）** → Shioaji 必須保留，NautilusTrader 致命傷成立 |
| 回測粒度 | **短期 bar-level 足夠，未來需 tick-level** → 短期不急，但需為效能升級鋪路 |
| Rust 意願 | **有興趣但時間有限** → 不能一次性大投入，需漸進式 |

---

## 七、結論：漸進式 Rust 混合架構（Phased Plan B）

**NautilusTrader 排除**：台灣+加密貨幣雙市場需求下，缺 Shioaji adapter 是致命傷，自建成本（4-8 週 Rust Gateway）比直接改 librae 還高，且喪失架構控制權。

### 推薦路線圖

```
Phase 0（現在）：Python 快速優化                    [1-2 週]
  → multiprocessing 參數掃描
  → Numba JIT 加速 equity calc
  → 不改架構，立即見效

Phase 1（Q3 2026）：Rust 學習 + executor 試點       [3-4 週]
  → 學 Rust + PyO3 基礎（The Rust Book + PyO3 guide）
  → 先重寫 make_fill() + calc_trade_pnl()（最小 scope，驗證可行性）
  → 用 maturin 建立 build pipeline
  → benchmark 對比 Python 版

Phase 2（Q4 2026）：hot loop Rust 化                [2-3 週]
  → 重寫 backtest bar-by-bar loop + process_actions()
  → 重寫 eval_equity()
  → Python strategy.on_bar() 透過 PyO3 callback 保持不變

Phase 3（2027）：tick-level 支援                    [視需求]
  → Rust tick engine（OrderBook matching）
  → 此時已有 Rust 基礎，開發速度會快很多
```

### 為什麼這條路線最適合

1. **時間有限 → 漸進式**：每個 Phase 獨立可交付，不會被底層工程拖住策略開發
2. **台灣市場 → 保留 Shioaji**：Broker adapter 層完全不動
3. **未來 tick-level → Rust 鋪路**：Phase 1-2 的投入在 Phase 3 直接複用
4. **雙市場 → 架構自主**：CostModel 的台灣當沖稅、期貨保證金邏輯完全自控
5. **Rust 學習 → 長期收益**：技能可複用於其他效能敏感專案

### 不建議的路線
- **全面重寫 librae 為 Rust**：ROI 太低，~80% 的程式碼（config/data/db/broker/notification）是 I/O bound，改 Rust 無意義
- **同時進行 A + B**：資源分散，選定一條路線後專注執行
