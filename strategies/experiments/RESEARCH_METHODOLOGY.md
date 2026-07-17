# 單一資產策略研究框架

`strategies/single_asset/` 每個子資料夾是一個獨立的策略家族：一支研究腳本 + 一支 `# @strategy` 部署檔（`BacktestService` 實際執行的檔案）。本文件描述**從因子分析到策略生成的共同流程與慣例**，不記錄任何特定家族目前用了哪些資產、參數或結論——這些屬於實驗結果，見各家族資料夾自己的 `report.md`。

## 流程總覽

```
① 資產/資料層  →  ② 樣本切分  →  ③ 因子分析（多頻率）  →  ④ 頻率/持有期決定
                                                              ↓
⑦ 正式引擎交叉驗證  ←  ⑥ 風控疊加校準  ←  ⑤ 策略候選生成與比較
                                                              ↓
                                                    ⑧ 跨資產/跨regime穩健性驗證
```

### ① 資產與資料層

資產在 `utils/universe.py` 的 `TICKERS` 註冊表統一登記（market/symbol/timeframe），研究腳本一律透過 `utils/data.py` 的 `load_ohlcv()` 取資料，不直接寫死交易所/資料源呼叫。`TICKERS` 裡的 `timeframe` 只是起點假設，不是鎖死的常數——是否需要換一個基礎頻率重抓資料，見③。

單一實驗若需要跟全域註冊不同的設定（例如測某資產的其他 timeframe、暫時加一個不打算進全域註冊表的資產），在研究腳本內用 dict 展開覆寫局部副本，不要直接改 `TICKERS`：

```python
LOCAL_TICKERS = {**TICKERS, "BTC": {**TICKERS["BTC"], "timeframe": "4h"}}
```

全域註冊表只放多個實驗都會用到的穩定設定；一次性覆寫留在該實驗自己的腳本裡。

### ② 樣本切分

三段式：**IS-Train**（因子篩選）→ **IS-Val**（候選策略挑選/參數校準）→ **OOS**（盲測，全程禁止回頭調參）。切分點依資料長度而定，但順序不可變：OOS 只能驗證，拿 OOS 表現回頭挑因子或調參會讓後面所有「穩健性」都不成立。

### ③ 因子分析：頻率是待驗證的假設，不是預設常數

**`evaluate` 跟 `evaluate_horizons` 分工不同，不是同義詞可以互換**：`evaluate()` 是單一 horizon 的原語，一次呼叫只在資料已 stamp 的那一個 forward period 上跑；官方文件明講「forward_periods is not a metric knob... To compare horizons, build two panels and evaluate each」。橫掃多個 forward period（如 1h/4h/12h/24h）找因子邊際在哪個時間尺度上最穩定顯著，要用 `evaluate_horizons()`——它就是「對每個 horizon 重建 panel 再呼叫 evaluate」這個流程的安全封裝（避開 `compute_forward_return` 非 idempotent、手動重複呼叫容易漏 reset 的坑），輸出可以直接餵給 `fx.compare()`/`fx.multi_factor.bhy()`。**不要手刻 for-loop 重複呼叫 `evaluate()` 來模擬橫掃**——這一步的產出決定④，不是拿來替一個已經定案的頻率背書。`evaluate()` 單獨使用的正確時機是④定案某個 horizon 之後，在那一個固定頻率上做更深的診斷（`ic_ir`/`spanning_alpha`等），不是用於篩選階段。

**橫掃不只是橫掃 forward period，K 線的基礎頻率本身也可以換**：若在起始選定的基礎頻率（如 1H）上，候選因子在所有 forward period 都不顯著、或穩定性診斷（見下方 `oos_decay`）沒過，不代表這個因子/家族沒救——下一步是換一個更粗或更細的基礎頻率（如 4H、1D）重新取資料、重新橫掃，而不是就此判定沒有希望，也不是死守最初選定的頻率硬做下去。實驗過程中換過哪些基礎頻率、為什麼留下最終這個，在報告裡用一兩行簡單註記交代即可（例如：「已測試 1H/4H：1H 上兩個候選因子在所有 forward period 皆不顯著，4H 上 mom_col 於 12h forward period 顯著且 `oos_decay` 通過，故採用 4H」），不需要每個嘗試都寫成獨立章節或保留失敗嘗試的完整輸出。

**多重檢定：因子 × 頻率（含基礎頻率）× 資產的組合本身就是網格搜尋**，跨這些維度比較 p-value 一律要校正，未校正的 p-value 會系統性高估顯著性。**先判斷這個決策要的是 FWER 還是 FDR，再挑工具，不是先挑工具再將就**：

- **FWER**（控制「至少挑錯一次」的機率）：適合「掃過一個網格挑單一贏家」的決策，例如④從③的 horizon×base_tf 掃描裡選一個頻率部署、或⑧檢查某個因子/regime 依賴是否只在決策資產上顯著。**factrix 公開 API 目前沒有 FWER 工具**——`factrix/_stats/multiple_testing.py` 雖然有 `holm_step_down`/`bonferroni`，但那是底線開頭的 private module，未被 `factrix/__init__.py` 或公開的 `factrix/stats/` 引用，不是穩定 API，不要依賴。這種情境用 `utils/stats.py` 的手刻 `holm_bonferroni`（Holm step-down）是目前唯一選項，屬於正當的 factrix 缺口，不是偷懶。
- **FDR**（控制「誤判佔比的期望值」）：適合「篩一批因子、把通過的全部留著用」的情境（例如 spanning_alpha/greedy_forward_selection 之後的一批候選因子）。這裡**優先用 factrix 現成工具**：`fx.stats.bhy_adjusted_p` 是吃純量 p-value 陣列的最輕量 primitive，不需要保留 `EvaluationResult`；需要依 horizon/資產等結構分組校正時用 `fx.multi_factor.bhy`/`bhy_hierarchical`/`partial_conjunction`。

**不要把 FDR 工具當成 FWER 工具的免手刻替代品**——兩者是不同的統計保證（FWER 比 FDR 嚴格），選錯了會系統性放寬顯著性門檻卻不自知。手刻校正（不管是因為需要 FWER、還是其他 factrix 沒覆蓋的情境）一旦被使用，**必須在該家族的 `report.md` 裡注記用了手刻版本、對應哪個 factrix 缺口，以及為什麼現成工具不適用**——這不是形式主義，因為手刻版本沒有 factrix 自己的正確性測試覆蓋，且校正方法本身是會影響結論的方法論選擇，必須留下痕跡讓人能覆核，不能悄悄替換。

**主流必看指標**（不管家族/資產是什麼，每次因子分析都先跑這一輪，是判斷「這個因子值不值得往下走」的基本盤）：

- 方向性/相關性：單資產時序用 `directional_hit_rate`，跨資產橫截面用 `ic`——依資料形狀擇一，不是兩個都要。
- 多頻率橫掃：`evaluate_horizons`（見上）。
- 多重比較校正：先定 FWER/FDR 再選工具（見上）。
- 穩定性快篩：`oos_decay`——IS 內部就先做一次切分檢查，過了再進到 IS-Val，不要等 IS-Val 才發現這個邊際本身就不穩。

**進階分析：依實際問題探索，不是每次都要跑完**。先查 `fx.list_metrics()` / `fx.metrics_summary()` 看有沒有現成 metric 能回答手上的具體問題，而不是不管什麼情境都套用固定的一套——這個列表是「遇到這類問題時可以去看」的方向，不是待辦清單：

- 「這個因子邊際夠不夠穩定、還是雜訊？」→ `ic_ir`（IC 均值/標準差，Sharpe 式穩定性）、`rank_turnover`（排名穩定性）。
- 「打完交易成本還剩多少邊際？」→ `tradability` family（`net_spread`、`breakeven_cost`、`notional_turnover`）——不用跑完整回測模擬器，因子篩選階段就能先過濾掉扛不住成本的因子。
- 「這個新因子/新資料源是不是白做工，跟既有因子重複？」→ `spanning_alpha`（控制既有因子後還有沒有獨立 alpha）、`greedy_forward_selection`（多因子逐步篩選）——比單純比較兩個策略的 Sharpe 更嚴謹。
- 「分桶後報酬是否單調，不只是方向相關？」→ `monotonicity`、`quantile_spread`（需要足夠橫截面樣本，`min_assets` 門檻通常 5~10；資產數不足時 `by_slice`/`compare` 的描述性排行榜仍是唯一選項）。
- 「訊號稀疏、想用事件研究角度分析？」→ `event_hit_rate`/`event_ic`/`profit_factor`/`signal_density`（`event_quality` family）、`caar`/`bmp_z`——跟 `utils/mfe_mae.py` 的事件取樣同一種資料形狀。
- 「想用跨資產迴歸角度交叉驗證？」→ `fm_beta`/`pooled_beta`（Fama-MacBeth）、`common_beta`——資產數夠多時可與 `ic()`/`predictive_beta` 互相印證。

### ④ 頻率與持有期決定

這一步的輸入是③橫掃出來的結果（含換過基礎頻率後的結果），不是研究者的經驗值。若某因子只在特定頻率顯著，策略的進出場邏輯應對齊那個時間尺度；若因子在所有已嘗試的頻率下都不穩定，代表它可能不夠格進入⑤，而不是隨便挑一個頻率湊合。常見錯誤是倒過來做：先憑經驗定頻率，因子分析只做事後驗證——若報告裡出現這種順序倒置，應在結論明確標註，而不是含混帶過。

### ⑤ 策略候選生成與比較

依④決定的頻率，構造 2-3 個候選邏輯（含至少一個不加額外濾鏡的基準，方便判斷濾鏡是否真的加值），在 IS-Val 上比較，選 Sharpe（或其他既定指標）最高者。挑選過程不可用 OOS。

### ⑥ 風控疊加校準（SL/TP 等）

若策略原本沒有停損停利，疊加前用 **IS 進場事件的 MAE/MFE 分布**反推（如 SL=75th percentile 逆向偏移、TP=50th percentile 順向偏移），而不是對 SL×TP 網格窮舉——後者本質是又一輪多重檢定。校準只用 IS，IS-Val/OOS 只驗證。

### ⑦ 正式引擎交叉驗證

所有回測數字最終以 `utils/engine_check.run_engine_cross_check()`（呼叫 `BacktestService.run()`，讀取磁碟上實際的策略檔）為準，不能只信研究腳本裡手刻的模擬器——手刻版本容易漏掉手續費/滑價、時間框架轉換慣例（ccxt 小寫 vs 引擎大寫，見 `utils/universe.to_engine_timeframe()`）等細節。手刻模擬器只適合③的快速迭代，不適合對「策略是否可交易」下結論。

### ⑧ 跨資產/跨 regime 穩健性驗證

決策只在單一「決策資產」上做；決定後，同一組參數原封不動套到其他資產/其他 regime 上比較表現，**不重新調參**。若某個因子/regime 依賴性只在決策資產上顯著、換資產就消失（多重檢定校正後，見③），代表那是決策資產特有的雜訊，不是可泛化的市場結構。

## 已知的框架級陷阱

- **非連續交易市場**：假設 24/7 連續交易的因子套到有固定交易時段、有隔夜/假日缺口的市場時語意會不對齊，需要交易時段感知（session-aware）版本，否則數字只能當框架可運作的煙霧測試。
- **跨市場的情緒/總經代理變數不通用**：某市場的情緒/總經序列套到結構不同的市場、退回中性 fallback 時，濾鏡形同虛設，回測結果只測到了濾鏡以外的子集邏輯。
- **外部資料源的可交易性缺口**：離線研究能取得的額外資料，若正式引擎的執行環境無網路存取，部署後讀不到——需要引擎支援「額外欄位注入」，資料源解決了不代表能上線。

## 目錄結構

- `strategies/single_asset/<family>/`：每個策略家族一支研究腳本 + 一支 `# @strategy` 部署檔 + 一份 `report.md`（研究結果，非框架文件，內容以腳本執行輸出為準）。
- `strategies/utils/`：共用工具層——資產註冊（`universe.py`）、資料存取（`data.py`、`cached_kline.py`）、因子庫（`factors.py`）、regime/外部資料（`regime.py`、`funding.py`、`cross_asset.py`、`open_interest.py`）、統計工具（`stats.py`）、MAE/MFE 與 SL/TP 重播（`mfe_mae.py`、`backtest_sim.py`）、跨資產面板組裝（`panel.py`）、正式引擎交叉驗證（`engine_check.py`）。
