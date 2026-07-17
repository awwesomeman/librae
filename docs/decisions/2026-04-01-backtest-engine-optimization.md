# 2026-04-01 — 回測引擎與資料層優化方向

> 狀態：accepted（部分已實作）
> 更新：2026-04-04 大部分項目已落地
> 注記：已完成項目：short bugs（proceeds + exit tax + ctx.cash）、cache trim (#7)、Context slots (#3)、position scaling + partial close。待實作：SL/TP 內建 (#4)、precompute bars numpy (#2)、ctx.bars_back (#5)、dry-run 增強 (#6)。詳見 [enhance_librae](../plans/enhance_librae.md)

## 核心定位

**librae 和 vectorbt 是兩條並存的路線，不該試圖把一個改成另一個。**

| 引擎 | 定位 | 適用場景 |
|------|------|---------|
| librae (bar-by-bar) | 可讀性 + 複雜邏輯支持 | 含狀態的策略、多資產、自訂風控、成本模型、正式回測寫入 DB |
| vectorbt | 極速向量化 | 大規模參數掃描、簡單 signal-based 策略、<1s 目標 |

如果目標是 <1s 就直接用 vectorbt；librae 不需要追求這個數量級。

---

## 拒絕的優化方向

| 方向 | 拒絕理由 |
|------|---------|
| **Numba 化** | 代價最高，等於重寫 Strategy 介面（on_bar 的 Context/Action 都是 Python object） |
| **Structured array 取代 dict** | dict lookup 不是真正瓶頸，收益有限 |

---

## 引擎層優化

### 值得做：效能

#### 1. 砍掉 per-bar Position snapshot allocation（~30%）
- **位置**：`librae/backtest/engine.py` `_eval_equity()` (line 407-434)
- **問題**：每根 bar 對每個 open position 都 new 一個 frozen `Position` dataclass 給 Context
- **改法**：只在 position 狀態真正變化時才重建 snapshot，否則重用上一根的
- **注意**：Context.positions 合約是 frozen，所以只要 position 沒變就可以 reuse

#### 2. `_precompute_bars()` 改用 numpy array + lightweight view（~20%）
- **位置**：`librae/backtest/engine.py` line 390-400
- **現狀**：`cross.to_dict(orient="index")` 已是一次性預算（有 WHY comment），但 dict-of-dict 記憶體開銷大
- **改法**：用 numpy structured array 或直接用 DataFrame `.values` + column index mapping
- **適用時機**：>100k bars 或 >50 symbols 時效益明顯

#### 3. Context 改 `__slots__`（~10%）
- **位置**：`librae/core/strategy.py` line 30-51
- **改法**：加 `@dataclass(frozen=True, slots=True)` (Python 3.10+)

### 值得做：功能

#### 4. 引擎層內建 SL/TP/Trailing Stop ⭐ 高優先
- **動機**：TrendMaster 實驗中，strategy 自己實作的 trailing stop 結果與 vectorbt 差異巨大（Sharpe 0.22 vs 0.87），因為 `on_bar` 在 bar 結束時才執行，漏掉 intra-bar 的 high/low 觸發
- **現狀**：每個策略都要自己寫 `_calc_sl`、`_check_sl_hit`、`_update_trailing`，重複且容易有 bug
- **建議**：
  - 引擎在 `on_bar` **之前**用 high/low 檢查 stop 觸發，與 vectorbt 行為一致
  - Strategy 的 Action 擴展：`Action(type="buy", sl=0.03, tp=0.15, trail=True)`
  - 引擎層維護 position-level 的 stop state（trail extreme、SL/TP price）
  - Strategy 保持 stateless 或 minimal state
- **效益**：消除各策略重複實作 stop 的 bug 源；librae 與 vectorbt 結果可互相驗證

#### 5. `ctx.bars_back(n)` — 暴露歷史 bar
- **現狀**：`ctx.bar` 只有當前 bar，策略要做 lookback 得自己維護 deque
- **建議**：Context 提供 `ctx.bars_back(n)` 返回前 n 根 bar 的 dict list，或 `ctx.history` 作為 rolling window
- **注意**：不是所有策略都需要，可 opt-in（引擎只在 strategy 宣告 `needs_history = True` 時才保留 buffer）

#### 6. dry-run 輸出增強
- **現狀**：`--dry-run` 只印一行 summary（trades, sharpe, mdd, ret）
- **建議**：加印月度 return 表 + top/bottom 5 trades，方便快速判斷策略行為是否合理

---

## 資料層優化

### 7. `fetch_ohlcv` cache 未 trim date range ⭐ 高優先
- **位置**：`data/binance.py`
- **問題**：cache 儲存了之前 fetch 過的所有資料。請求 `end="2025-06-30"` 時，若 cache 已有到 2026-03 的資料，會全部返回，不會依 `end` 截斷
- **影響**：TrendMaster IS 回測跑了 19,694 bars（含 OOS 資料），直到手動加 trim 才修正。這類 bug 會在不知情的情況下造成資料洩漏
- **修復**：`fetch_ohlcv` 返回前依 `start`/`end` 過濾 DataFrame

---

## 資料庫優化

> 資料庫相關優化已移至 `2026-03-31-database-schema-optimization.md` P0 區段統一管理，
> 包含：schema migration 自動化、backtest_runs 存 params JSONB、OHLCV 去重、equity_curve 加 strategy_name。

---

## 發現的潛在問題

### ✅ BUG: Short position 的 close proceeds 計算錯誤
- **已修復**：`38e450d` — `close_position` 對 short 改用 `entry_notional + gross_pnl - exit_costs`
- 同時修復 exit tax bug：`is_sell=(side == "long")`，buy-to-cover 不再課稅

### ✅ BUG: Short position 期間 ctx.cash 偏低
- **設計決策**：維持 collateral 模式（扣全額當保證金）。這是 QuantConnect 的做法，保守但安全。
- equity 計算正確，ctx.cash 偏低是此模式的預期行為，已在 plan 中文件化。

### ✅ 缺少 Short position 測試
- **已修復**：`38e450d` — 新增 25 個測試（`tests/engine/test_position_scaling.py`），涵蓋 short PnL、scaling、partial close、edge cases

### 缺少效能 benchmark
- **現狀**：仍未建立 pytest-benchmark 基線
- **原因**：本次聚焦功能正確性（short/scaling），效能優化留待 Phase 3
- **前置條件**：需安裝 pytest-benchmark，選定標準 dataset（4300 bars / 52k bars）

---

## 優先順序（引擎 + 資料層）

| 優先 | 項目 | 類型 | 狀態 | 未做原因 |
|------|------|------|------|---------|
| P0 | #7 cache trim date range | Bug fix | ✅ `66c727e` | |
| P1 | #4 引擎內建 SL/TP/Trailing | 功能 | 待實作 | 需擴展 Action（sl/tp/trail 欄位）+ 引擎 stop state，獨立專案 |
| P1 | Short proceeds + exit tax + ctx.cash | Bug fix | ✅ `38e450d` | |
| P2 | #1 Position snapshot reuse | 效能 | 移除 | Position 是 frozen dataclass，unrealized_pnl 每 bar 都變，無法有效 reuse |
| P3 | #2 precompute bars numpy | 效能 | 待實作 | 需先建 pytest-benchmark 基線驗證收益 |
| P3 | #3 Context slots | 效能 | ✅ `66c727e` | |
| P3 | #5 ctx.bars_back | 功能 | 待實作 | 獨立功能，目前策略不需要 lookback |
| P3 | #6 dry-run 輸出增強 | UX | 待實作 | 非核心，等使用需求出現 |
| — | Position scaling + partial close | 功能 | ✅ `38e450d` | |

> DB 相關優先順序見 `2026-03-31-database-schema-optimization.md`。

---

## 相關 Decisions

- `2026-03-26-backtest-performance-optimization.md` — Lumibot 時代的優化（已過時，但 Numba 不做的結論仍成立）
- `2026-03-27-backtest-engine-refactor.md` — 引擎從 POC 重構為現行架構
- `2026-03-26-platform-architecture.md` — vectorbt 做研究層、自建 runner 做高保真的分工定位
