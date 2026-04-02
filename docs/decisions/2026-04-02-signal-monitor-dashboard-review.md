# 2026-04-02 — Signal Monitor 儀表板審查與改進方案

> 狀態：accepted
> 來源：以資深 Quant / CFA 視角，對照學術文獻與業界最佳實踐（Grinold & Kahn, de Prado, Macrosynergy, Alphalens）進行審查
> 核心原則：此儀表板衡量的是「訊號 (Signal) 預測力」，非「策略 (Strategy) 交易績效」，兩者必須分開

## 前提

`signal_outcomes` 表尚未建入 DB schema。現有 `strategy_signals` 僅有 `signal_strength`（±1.0 離散值）。以下審查假設該表會按設計建出。

### 訊號模型

**方向無關設計：** 系統不假設訊號值的方向語意。訊號值可能來自趨勢模型（正=看多）、波動度模型（值=預測波動）、風險模型（負=風險下降）等。系統只衡量「訊號觸發後，市場發生了什麼」，由分析結果揭示訊號是正指標、反指標或噪音。

**資料儲存：**

| 項目 | 決定 |
|------|------|
| signal_value 範圍 | 任意實數（正、負、零皆可） |
| NaN 語意 | 「這個時間點沒有訊號」 |
| 0 語意 | 有效觀測值（如「模型輸出中性」），不等於 NaN |
| 儲存 | signal_outcomes **只存有訊號的行**（signal_value IS NOT NULL），NaN 時間點不寫入 |
| Baseline 比較 | 不在 signal_outcomes 做。需要時在 factorlib 診斷頁用原始 OHLCV 即時算 |
| 門檻過濾 | 使用者在分析端（Streamlit）自行設門檻，低於門檻的視為排除。DB 存原始值不預處理 |

**指標定義（方向無關）：**

| 指標 | 計算 | 語意 | 布林(恆=1) | 連續 |
|------|------|------|-----------|------|
| Win Rate | `AVG(fwd_return > 0)` | 訊號後價格上漲比例（非「正確率」） | 有效 | 有效 |
| Mean Fwd Return | `AVG(fwd_return)` | 訊號後平均報酬 | 有效 | 有效 |
| MFE | `max(high - entry)` within window | 訊號後最大上漲（統一做多視角） | 有效 | 有效 |
| MAE | `max(entry - low)` within window | 訊號後最大下跌（統一做多視角） | 有效 | 有效 |
| IC | `corr(signal_value, fwd_return)` | 正=正指標，負=反指標，≈0=噪音 | NaN（無變異） | 有效 |
| IC_IR | `mean(rolling_IC) / std(rolling_IC)` | IC 穩定性 | NaN | 有效 |

IC 系列指標一律提供，布林訊號（signal_value 無變異）時回傳 NaN，不報錯。

**所有指標旁必須顯示 N（樣本數）**，讓門檻過濾的影響可見。

---

## Bug 修正

### B1 — Edge Profile 三個 Panel 未使用 `$obs_window` 變數

**現況：** Rolling MFE p50 / Rolling MAE p75+p95 / MFE÷MAE Ratio 標題都寫 `$obs_window bars`，但 SQL 硬寫 `mfe_24` / `mae_24`，使用者切換變數無效果。

**修正：** SQL 改用動態欄位插值：

```sql
-- 三個 panel 統一改法
mfe_$obs_window AS mfe
mae_$obs_window AS mae
```

### B2 — Win Rate Summary 未標注 Horizon

**現況：** Summary card 的 Win Rate 固定用 `fwd_return_24`，但標題只寫 "Win Rate"，易誤解為整體勝率。

**修正：** 標題改為 `Win Rate (T+24)`，或改為跟 `$obs_window` 連動。

### B3 — MFE/MAE Summary vs Rolling 使用不同統計量

**現況：**
- Summary card：`AVG(mfe) / AVG(mae)` = ratio of means（受極端值拉偏）
- Rolling panel：`p50(mfe) / p75(mae)` = median vs tail（穩健）

兩者衡量不同東西，容易在同一畫面上造成混淆。

**修正方案：**
- Summary 改用 `PERCENTILE_CONT(0.50) ... (mfe) / PERCENTILE_CONT(0.50) ... (mae)` 即 median ratio
- Description 中明確標注 Summary = median/median，Rolling = p50/p75 與 p50/p95

---

## 方法學改進

### M1 — IC 改用 Spearman Rank Correlation

**現況：** 所有 IC panel 用 PostgreSQL `corr()` = Pearson。

**問題：**
1. Pearson IC 對加密貨幣厚尾分布的極端值高度敏感
2. 若 `signal_value` 為 ±1 離散值，Pearson correlation 退化為 point-biserial，只能衡量做多/做空平均報酬差異，無法捕捉訊號強度與報酬幅度的關聯
3. 業界標準（Grinold & Kahn、Alphalens、de Prado）一致推薦 Spearman Rank IC

**修正：** 以 SQL 計算 Spearman rank correlation：

```sql
-- Spearman IC = Pearson correlation of ranks
WITH ranked AS (
  SELECT
    RANK() OVER (ORDER BY signal_value) AS rank_signal,
    RANK() OVER (ORDER BY fwd_return_24) AS rank_return
  FROM signal_outcomes
  WHERE ...
)
SELECT corr(rank_signal, rank_return) AS "Spearman IC" FROM ranked
```

> **附註：** 若訊號長期維持 ±1 離散值，Spearman 與 Pearson 結果相同（因為排名只有兩層）。此修正的價值在於：當訊號未來擴展為連續值（如信心度 0.3~1.0）時，IC 計算自動正確。建議在 description 中說明離散訊號下的解讀限制。

### M2 — 新增 Balanced Accuracy 指標

**現況：** 只有 Win Rate = `P(sign(signal) == sign(return))`。

**問題：** 在趨勢市場中（如長期牛市），一個永遠做多的「訊號」也會有高 Win Rate。Win Rate 無法區分「真正的預測力」和「順著市場偏差」。

**修正：** 新增 Balanced Accuracy stat panel：

```sql
-- Balanced Accuracy = (TPR + TNR) / 2
-- 免疫市場方向偏差
WITH base AS (
  SELECT signal_value, fwd_return_24
  FROM signal_outcomes
  WHERE ... AND fwd_return_24 IS NOT NULL
),
rates AS (
  SELECT
    AVG(CASE WHEN signal_value > 0 AND fwd_return_24 > 0 THEN 1.0 ELSE 0.0 END)
    / NULLIF(AVG(CASE WHEN fwd_return_24 > 0 THEN 1.0 ELSE 0.0 END), 0) AS tpr,
    AVG(CASE WHEN signal_value < 0 AND fwd_return_24 < 0 THEN 1.0 ELSE 0.0 END)
    / NULLIF(AVG(CASE WHEN fwd_return_24 < 0 THEN 1.0 ELSE 0.0 END), 0) AS tnr
  FROM base
)
SELECT (tpr + tnr) / 2.0 AS "Balanced Acc" FROM rates
```

**Threshold：** 與 Win Rate 相同（R<0.35, Y<0.45, G≥0.5），但 Balanced Accuracy = 0.5 真正代表隨機。

**位置：** Summary row，緊接在 Win Rate 旁。

### M3 — Rolling IC 的 Green Threshold 過嚴

**現況：** Rolling IC 的 green ≥ 0.1，但 IC Around Event 的 green ≥ 0.05。

**問題：** 加密貨幣 H1 訊號典型 IC 範圍 0.03–0.10（Alphalens 實證）。IC=0.07 的穩定訊號在 Rolling IC 上會被標黃，但在 IC Around Event 上標綠，前後矛盾。

**修正：** Rolling IC 的 green threshold 從 0.1 降為 **0.05**，與 IC Around Event 一致。

### M4 — Rolling Window 預設值建議調高

**現況：** `$window` 預設 30。

**問題：** 相關係數在 n=30 時的標準誤約 ±0.18，統計信心不足。n=50 時標準誤降至 ±0.14，更穩健。

**修正：** `$window` 預設值從 30 改為 **50**，保留 30/100 作為可選項：`query: "50,30,100"`。

---

## 新增指標

### N1 — IC Information Ratio (IC_IR) — 訊號穩定性核心 KPI

**理由：** Grinold & Kahn 基本法則 `IR = IC × √BR` 的核心前提是 IC 穩定。IC_IR = mean(IC) / std(IC) 衡量的正是這個穩定性。一個 IC=0.03 但 IC_IR=0.8 的訊號，遠優於 IC=0.08 但 IC_IR=0.2 的訊號。

| IC_IR | 解讀 |
|-------|------|
| < 0.3 | 不可靠 |
| 0.3–0.5 | 可接受 |
| > 0.5 | 強且穩定 |

**實作：** Summary row 新增 stat panel：

```sql
WITH rolling AS (
  -- 複用 Rolling IC 的邏輯，計算每個時間點的 IC
  ...
)
SELECT AVG(ic) / NULLIF(STDDEV(ic), 0) AS "IC_IR"
FROM rolling
```

**Threshold：** R<0.3, Y<0.3, G≥0.5

### N2 — Signal Autocorrelation — 訊號翻轉特性

**理由：** 高自相關 = 訊號穩定、低換手（降低交易成本）。低自相關 = 頻繁翻轉（可能是雜訊或 mean-reversion 型態）。這是將訊號轉化為策略時決定交易成本假設的關鍵參數。

**實作：** Signal Quality row 新增 bar chart，顯示 lag=1,2,3,6,12 的自相關：

```sql
WITH base AS (
  SELECT signal_value,
    LAG(signal_value, 1) OVER (ORDER BY signal_ts) AS lag1,
    LAG(signal_value, 2) OVER (ORDER BY signal_ts) AS lag2,
    LAG(signal_value, 3) OVER (ORDER BY signal_ts) AS lag3,
    LAG(signal_value, 6) OVER (ORDER BY signal_ts) AS lag6,
    LAG(signal_value, 12) OVER (ORDER BY signal_ts) AS lag12
  FROM signal_outcomes
  WHERE ...
)
SELECT 'Lag 1' AS lag, corr(signal_value, lag1) AS "Autocorr" FROM base WHERE lag1 IS NOT NULL
UNION ALL SELECT 'Lag 2', corr(signal_value, lag2) FROM base WHERE lag2 IS NOT NULL
UNION ALL SELECT 'Lag 3', corr(signal_value, lag3) FROM base WHERE lag3 IS NOT NULL
UNION ALL SELECT 'Lag 6', corr(signal_value, lag6) FROM base WHERE lag6 IS NOT NULL
UNION ALL SELECT 'Lag 12', corr(signal_value, lag12) FROM base WHERE lag12 IS NOT NULL
```

> **附註：** 若 signal_value 為 ±1 離散值，自相關衡量的是「連續同方向訊號的比例」，仍然有意義。

### N3 — Signal Equity Curve 更名與說明修正

**現況：** 標題 "Signal Equity Curve"，描述 "Cumulative PnL"。

**問題：** "PnL" 和 "Equity Curve" 都是策略層級用語，暗示考慮了部位管理、滑價、手續費，但此面板完全不考慮這些。

**修正：**
- 標題改為 **"Cumulative Signal Return"**
- Description 改為：`Cumulative signal_value × fwd_return at each holding horizon. No position sizing, slippage, or fees — this measures pure signal edge accumulation, not strategy PnL.`

---

## 未納入（暫不建議）

| 指標 | 理由 |
|------|------|
| Signal Decay Curve (IC by lag) | IC Around Event + Win Rate by Horizon 已部分涵蓋。當訊號擴展為連續值且有更多 horizon 欄位時再加 |
| Quantile Return Spread | 需要連續訊號值才有意義。±1 離散訊號只有兩個 quantile = 方向比較，已被 Win Rate 涵蓋 |
| Confusion Matrix | 資訊與 Win Rate + Balanced Accuracy 重疊 |
| Signal Distribution Histogram | 離散訊號只有兩個值，無分布可看。連續化後再加 |

---

## 改動摘要

| 類型 | ID | 項目 | 影響範圍 |
|------|-----|------|----------|
| Bug | B1 | `$obs_window` 未生效 | 3 panels SQL |
| Bug | B2 | Win Rate 未標注 horizon | 1 panel title |
| Bug | B3 | MFE/MAE 統計量不一致 | 1 panel SQL + descriptions |
| 方法學 | M1 | IC → Spearman | 2 panels SQL |
| 方法學 | M2 | 新增 Balanced Accuracy | 新 panel |
| 方法學 | M3 | Rolling IC threshold 0.1→0.05 | 1 panel config |
| 方法學 | M4 | Window 預設 30→50 | 1 variable |
| 新增 | N1 | IC_IR | 新 panel |
| 新增 | N2 | Signal Autocorrelation | 新 panel |
| 新增 | N3 | Equity Curve 更名 | 1 panel title + desc |

## 架構決策：兩專案解耦 + 監控 vs 診斷分層

### 核心原則

1. **quant-strategy-lab 與 factorlib 解耦**：不互相 import，各自獨立部署
2. **哨兵與診斷分離**：Grafana 只做監控告警，深度分析搬到 factorlib 的 Streamlit
3. **重要指標公式一致**：兩邊各自實作，但 IC、Hit Rate 等核心公式定義保持對齊

### 專案邊界

```
quant-strategy-lab (交易系統)              factorlib (研究工具)
├── librae/metrics/                        ├── scoring/
│   └── performance.py ← 策略績效         │   ├── selection.py  ← IC、IC_IR (已有)
│       (核心公式與 factorlib 對齊)        │   └── timing.py     ← Hit Rate、Event CAAR (已有)
├── app/grafana/                           ├── builders.py       ← signal_feature_matrix (已有)
│   └── signal_monitor.json               ├── dashboard/
│       ← 哨兵 4 panels + alerting        │   ├── app.py        ← 現有因子排行榜
├── pipeline                               │   └── pages/
│   └── 寫 signal_metrics (4 值/訊號)     │       └── Signal_Diagnosis.py ← 新增
└── DB                                     └── 新增：MFE/MAE、Balanced Accuracy
    ├── signal_outcomes (原始資料)               (若通用性夠高，否則只在診斷頁實作)
    └── signal_metrics (哨兵用)
```

**不互相 import 的理由：**
- 交易系統的部署週期和研究工具不同，耦合會互相拖累
- factorlib 用 Polars，quant-strategy-lab 用 Pandas，強行整合增加轉換成本
- 「做分析時去拿 factorlib 來用」是人的動作，不是系統的依賴

### Grafana 哨兵層 (quant-strategy-lab)

只回答一個問題：**「現在需不需要介入？」**

Mode 固定為 sim（hardcode `source='sim'`）。Strategy 代表訊號邏輯，可搭配不同 symbol，單選快速切換。

**Template variables：**

| 變數 | 用途 | 預設 | 可選值 |
|------|------|------|--------|
| `$strategy` | 策略（訊號邏輯）篩選 | query from DB | — |
| `$symbol` | 標的篩選（單選切換） | query from DB | — |
| `$n` | Forward horizon (bars) | 24 | 6, 12, 24 |
| `$k` | Rolling window (signals) | 50 | 30, 50, 100 |
| `$expected_direction` | 正指標(1) / 反指標(-1) | 1 | 1, -1 |

**Snapshot row (7 stat panels)：** 排列順序依 UX 優先級 — 左到右：最緊急 → 參考資訊，同寬度分組（重要 w=4，次要 w=3）。

| 順序 | Panel | 寬度 | 計算 |
|------|-------|------|------|
| 1 | Unrealized PnL | w=4 | `$expected_direction × (current_close − entry_close) / entry_close` |
| 2 | Mean Fwd Return (T+n) | w=4 | `$expected_direction × AVG(fwd_return_$n)` |
| 3 | Edge Ratio (T+n) | w=4 | `AVG(mfe_$n) / AVG(mae_$n)`，MFE/MAE 從做多視角 |
| 4 | Last Signal Age | w=3 | `NOW() - MAX(signal_ts)`，單位：小時 |
| 5 | N (Signals) | w=3 | `COUNT(*)`，所有指標的樣本數 context |
| 6 | Signal Value | w=3 | 最新一筆 signal_value |
| 7 | Timeframe | w=3 | 固定顯示 H1（hardcode） |

**Trend row (4 timeseries panels)：**

| Panel | 寬度 | 計算 |
|-------|------|------|
| Price & Signals | w=12 | OHLCV close（左軸）+ signal_value scatter points（右軸，橘色） |
| Cumulative Signal Return (T+n) | w=12 | `SUM($expected_direction × fwd_return_$n) OVER (ORDER BY signal_ts)` |
| Rolling k Mean Return (T+n) | w=12 | `AVG($expected_direction × fwd_return_$n) OVER (ROWS $k PRECEDING)` |
| Rolling k Edge Ratio (T+n) | w=12 | `AVG(mfe_$n) / AVG(mae_$n) OVER (ROWS $k PRECEDING)` |

Price & Signals 和 Cumulative Signal Return 同一行並排；Rolling Mean Return 和 Rolling Edge Ratio 同一行並排。

共 **11 panels**（7 stat + 4 timeseries）。全部純 SQL（window function），不需要 pipeline 額外寫入。

**Alerting rules：**

使用者部署時已知訊號是正指標或反指標，透過 `$expected_direction` 配置。

| 指標 | Alert 條件 | 嚴重度 |
|------|-----------|--------|
| Last Signal Age | > 48hr | Critical — 系統可能掛了 |
| Mean Fwd Return | `$expected_direction × mean_fwd_return < 0` 持續 2 個 window | Warning — edge 反轉 |
| Edge Ratio | < 1.0 持續 3 個 window | Warning — edge 消失 |

> **IC 不放哨兵的理由：** 布林訊號（signal_value 無變異）時 IC = NaN。哨兵必須對所有訊號類型都有效。IC 系列指標保留在 factorlib 診斷頁，連續訊號時自動生效，布林訊號時顯示 NaN。

### Streamlit 診斷層 (factorlib)

選 Streamlit 而非 Notebook 的理由：Notebook 是空白畫布，每次分析自己決定跑哪些 cell，結構性地鼓勵不一致。Streamlit 頁面載入即跑完整套診斷，如同飛行員 checklist。

Notebook 保留給探索性研究（試新指標、假設檢定、one-off 分析）。

**遷移策略：** 現有 Grafana Signal Monitor 的 15 個 panel 設計直接搬到 factorlib 的 Streamlit，不重新設計。後續在 Streamlit 上做本文件列出的 Bug 修正與方法學改進即可。

**先獨立頁面，再評估整合：**

```
factorlib/dashboard/
├── app.py                        ← 入口 + 導航
├── pages/
│   ├── 1_Leaderboard.py          ← 現有：因子排行 (MLflow)
│   ├── 2_Factor_Detail.py        ← 現有：單因子 drill-down
│   └── 3_Signal_Diagnosis.py     ← 新增：訊號診斷 (DataFrame/parquet)
└── charts.py                     ← 共用圖表元件
```

先作為獨立頁面加入現有 Streamlit app（共用基礎設施），不需要額外部署。如果後來資料載入方式差太多，再拆成獨立 app — 只需把頁面搬出來當 entry point，零成本。

**一頁式診斷流程：**

```
Signal Diagnosis
├── Sidebar: strategy / symbol / time range (st.query_params 同步 URL)
│
├── 1. Overview (同哨兵 4 指標，帶 context)
│     IC_IR | Win Rate | Balanced Acc | MFE/MAE | Density
│
├── 2. 預測力 — 訊號還能預測嗎？
│     IC Around Event | Win Rate by Horizon
│
├── 3. Edge 品質 — 抓到的行情夠大嗎？
│     Return per Bar | Rolling MFE/MAE breakdown
│
├── 4. 訊號行為 — 訊號本身有變嗎？
│     Autocorrelation | Signal Density trend
│
└── 5. 累積觀察
      Cumulative Signal Return (multi-horizon)
```

### factorlib 已有的指標（不需重寫）

| 指標 | factorlib 位置 | 對應本文件項目 |
|------|---------------|---------------|
| Spearman Rank IC | `scoring/selection.py` → `Rank_IC` | M1 |
| IC_IR | `scoring/selection.py` → `IC_IR` | N1 |
| Hit Rate (含 t-stat) | `scoring/timing.py` → `Hit_Rate` | Win Rate |
| Event CAAR | `scoring/timing.py` → `Event_CAAR` | IC Around Event |
| Turnover (rank autocorr) | `scoring/timing.py` → `Turnover` | N2 |
| Event Decay | `scoring/timing.py` → `Event_Decay` | Signal Decay |
| Profit Factor | `scoring/timing.py` → `Profit_Factor` | — |

**需新增至 factorlib：** MFE/MAE 分析、Balanced Accuracy（若通用性夠高）。

### quant-strategy-lab 側的 metric.py

`librae/metrics/performance.py` 保留用於**策略績效計算**（Sharpe、MDD、Profit Factor 等回測引擎輸出）。核心公式（如 Hit Rate、Profit Factor）定義與 factorlib 保持一致，但各自實作、不互相依賴。

**一致性管理：** 公式定義少（< 10 個重疊指標），變動頻率低，人工對齊即可。不值得為此建立共享套件或自動化同步。

### 指標計算分界線

| 留在 Grafana SQL (quant-strategy-lab) | 放 factorlib Streamlit |
|------|------|
| Rolling average / sum / count | Spearman IC (rank → corr) |
| LAG / LEAD (inter-signal gap) | IC_IR (mean / std of rolling IC) |
| 簡單 sign() 比對 (rolling win rate) | Balanced Accuracy (TPR / TNR) |
| **SQL 一行寫完、無領域歧義** | **定義複雜、需要完整診斷流程** |

## 相關決策

- [2026-03-26 Dashboard 資料範圍定義](2026-03-26-dashboard-data-scope.md) — Grafana vs Streamlit 分工
- [2026-03-31 資料庫 Schema 優化](2026-03-31-database-schema-optimization.md) — signal_outcomes 表需新建
