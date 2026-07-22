# 因子研究方法論

`strategies/<name>/`（因子驗證通過,已部署)或 `strategies/experiments/<name>/`（研究中/未通過)
每個資料夾：一支 `factor_research.py` + （通過驗證才有的）`strategy.py`/`utils.py`/`config.yaml`
+ 一份 `report.md`。本文件描述**從因子分析到策略生成的共同流程與慣例**，不記錄任何特定家族
目前用了哪些資產、參數或結論——那些屬於實驗結果，見各家族自己的 `report.md`。

## 流程總覽

```
1 資產/資料層  →  2 樣本切分  →  3 因子分析（多頻率）  →  4 頻率/持有期決定
                                                              ↓
7 正式引擎交叉驗證  ←  6 風控疊加校準  ←  5 策略候選生成與比較
                                                              ↓
                                                    8 跨資產/跨regime穩健性驗證
```

### 1 資產與資料層

資產在 `librae/config/symbols.yaml` 統一登記（market/data_source），研究腳本一律透過
`strategies.module.data.ohlcv.get_ohlcv()` 取資料，不直接寫死交易所/資料源呼叫。symbols.yaml
的 `timeframe` 只是起點假設，不是鎖死的常數——是否需要換一個基礎頻率重抓資料，見3。

單一實驗需要跟全域登記不同的設定（測其他 timeframe、暫時加一個一次性資產），在研究腳本內用
`get_ohlcv(symbol, timeframe, ...)` 的參數直接覆寫即可，不要改 `symbols.yaml`。

### 2 樣本切分

三段式：**IS-Train**（因子篩選）→ **IS-Val**（候選策略挑選/參數校準）→ **OOS**（盲測，全程禁止回頭調參）。切分點依資料長度而定，但順序不可變：OOS 只能驗證，拿 OOS 表現回頭挑因子或調參會讓後面所有「穩健性」都不成立。共用切分函式：`strategies.module.utils.split_is_val_oos()`。

### 3 因子分析：頻率是待驗證的假設，不是預設常數

**`evaluate` 跟 `evaluate_horizons` 分工不同，不是同義詞可以互換**：`evaluate()` 是單一 horizon 的原語，一次呼叫只在資料已 stamp 的那一個 forward period 上跑；官方文件明講「forward_periods is not a metric knob... To compare horizons, build two panels and evaluate each」。橫掃多個 forward period（如 1h/4h/12h/24h）找因子邊際在哪個時間尺度上最穩定顯著，要用 `evaluate_horizons()`——它就是「對每個 horizon 重建 panel 再呼叫 evaluate」這個流程的安全封裝（避開 `compute_forward_return` 非 idempotent、手動重複呼叫容易漏 reset 的坑），輸出可以直接餵給 `fx.compare()`/`fx.multi_factor.bhy()`。**不要手刻 for-loop 重複呼叫 `evaluate()` 來模擬橫掃**——這一步的產出決定4，不是拿來替一個已經定案的頻率背書。`evaluate()` 單獨使用的正確時機是4定案某個 horizon 之後，在那一個固定頻率上做更深的診斷（`ic_ir`/`spanning_alpha`等），不是用於篩選階段。

**橫掃不只是橫掃 forward period，K 線的基礎頻率本身也可以換**：若在起始選定的基礎頻率（如 1H）上，候選因子在所有 forward period 都不顯著、或穩定性診斷（見下方 `oos_decay`）沒過，不代表這個因子/家族沒救——下一步是換一個更粗或更細的基礎頻率（如 4H、1D）重新取資料、重新橫掃，而不是就此判定沒有希望，也不是死守最初選定的頻率硬做下去。報告裡用一兩行簡單註記交代換過哪些基礎頻率、為什麼留下最終這個即可，不需要每個嘗試都寫成獨立章節或保留失敗嘗試的完整輸出。

**多重檢定：因子 × 頻率（含基礎頻率）× 資產的組合本身就是網格搜尋**，跨這些維度比較 p-value 一律要校正，未校正的 p-value 會系統性高估顯著性。**先判斷這個決策要的是 FWER 還是 FDR，再挑工具，不是先挑工具再將就**：

- **FWER**（控制「至少挑錯一次」的機率）：適合「掃過一個網格挑單一贏家」的決策，例如4從3的 horizon×base_tf 掃描裡選一個頻率部署、或8檢查某個因子/regime 依賴是否只在決策資產上顯著。用 `factrix.stats.holm_adjusted_p`（Holm step-down）或 `romano_wolf_adjusted_p`（比 Holm 寬鬆但一樣控制 FWER，適合檢定間有相依性的情況）——兩者都是自 factrix `0.17.0` 起的公開 API（`pyproject.toml` 已鎖 `factrix>=0.17`），不要手刻。舊報告（寫於 factrix 0.17 之前）手刻的 `holm_bonferroni` 保留不動——歷史記錄不回頭改，但之後新的因子研究直接用公開 API。
- **FDR**（控制「誤判佔比的期望值」）：適合「篩一批因子、把通過的全部留著用」的情境（例如 spanning_alpha/greedy_forward_selection 之後的一批候選因子）。用 `fx.stats.bhy_adjusted_p`（吃純量 p-value 陣列的最輕量 primitive）；需要依 horizon/資產等結構分組校正時用 `fx.multi_factor.bhy`/`bhy_hierarchical`/`partial_conjunction`。

**不要把 FDR 工具當成 FWER 工具的替代品**——兩者是不同的統計保證（FWER 比 FDR 嚴格），選錯了會系統性放寬顯著性門檻卻不自知。

**主流必看指標**（不管家族/資產是什麼，每次因子分析都先跑這一輪，是判斷「這個因子值不值得往下走」的基本盤）：

- 方向性/相關性：單資產時序用 `directional_hit_rate`，跨資產橫截面用 `ic`——依資料形狀擇一，不是兩個都要。
- 多頻率橫掃：`evaluate_horizons`（見上）。
- 多重比較校正：先定 FWER/FDR 再選工具（見上）。
- 穩定性快篩：`oos_decay`——IS 內部就先做一次切分檢查，過了再進到 IS-Val，不要等 IS-Val 才發現這個邊際本身就不穩。

**進階分析：依實際問題探索，不是每次都要跑完**。先查 `fx.list_metrics()` / `fx.metrics_summary()` 看有沒有現成 metric 能回答手上的具體問題：

- 「這個因子邊際夠不夠穩定、還是雜訊？」→ `ic_ir`（IC 均值/標準差，Sharpe 式穩定性）、`rank_turnover`（排名穩定性）。
- 「打完交易成本還剩多少邊際？」→ `tradability` family（`net_spread`、`breakeven_cost`、`notional_turnover`）——不用跑完整回測模擬器，因子篩選階段就能先過濾掉扛不住成本的因子。
- 「這個新因子/新資料源是不是白做工，跟既有因子重複？」→ `spanning_alpha`（控制既有因子後還有沒有獨立 alpha）、`greedy_forward_selection`（多因子逐步篩選）——比單純比較兩個策略的 Sharpe 更嚴謹。
- 「分桶後報酬是否單調，不只是方向相關？」→ `monotonicity`、`quantile_spread`（需要足夠橫截面樣本，`min_assets` 門檻通常 5~10；資產數不足時 `by_slice`/`compare` 的描述性排行榜仍是唯一選項）。
- 「訊號稀疏、想用事件研究角度分析？」→ `event_hit_rate`/`event_ic`/`profit_factor`/`signal_density`（`event_quality` family）、`caar`/`bmp_z`。
- 「想用跨資產迴歸角度交叉驗證？」→ `fm_beta`/`pooled_beta`（Fama-MacBeth）、`common_beta`——資產數夠多時可與 `ic()`/`predictive_beta` 互相印證。

### 4 頻率與持有期決定

這一步的輸入是3橫掃出來的結果（含換過基礎頻率後的結果），不是研究者的經驗值。若某因子只在特定頻率顯著，策略的進出場邏輯應對齊那個時間尺度；若因子在所有已嘗試的頻率下都不穩定，代表它可能不夠格進入5，而不是隨便挑一個頻率湊合。常見錯誤是倒過來做：先憑經驗定頻率，因子分析只做事後驗證——若報告裡出現這種順序倒置，應在結論明確標註，而不是含混帶過。

### 5 策略候選生成與比較

依4決定的頻率，構造 2-3 個候選邏輯（含至少一個不加額外濾鏡的基準，方便判斷濾鏡是否真的加值），在 IS-Val 上比較，選 Sharpe（或其他既定指標）最高者。挑選過程不可用 OOS。

### 6 風控疊加校準（SL/TP 等）

若策略原本沒有停損停利，疊加前用 **IS 進場事件的 MAE/MFE 分布**反推（如 SL=75th percentile 逆向偏移、TP=50th percentile 順向偏移），而不是對 SL×TP 網格窮舉——後者本質是又一輪多重檢定。校準只用 IS，IS-Val/OOS 只驗證。共用工具：`strategies.module.factors.utils.mae_mfe_percentiles()`。

### 7 正式引擎交叉驗證

所有回測數字最終以 `librae.backtest.engine.Backtest` 為準（本 repo 的正式引擎,不需要另外的
`engine_check` 包裝層——研究腳本直接呼叫 `Backtest` 就是「正式引擎」本身),不能只信研究腳本裡
手刻的模擬器——手刻版本容易漏掉手續費/滑價等細節,只適合3的快速迭代。共用封裝：
`strategies.module.factors.utils.run_engine_backtest()`。

### 8 跨資產/跨 regime 穩健性驗證

決策只在單一「決策資產」上做；決定後，同一組參數原封不動套到其他資產/其他 regime 上比較表現，**不重新調參**。若某個因子/regime 依賴性只在決策資產上顯著、換資產就消失（多重檢定校正後，見3），代表那是決策資產特有的雜訊，不是可泛化的市場結構。

## 已知的框架級陷阱

- **非連續交易市場**：假設 24/7 連續交易的因子套到有固定交易時段、有隔夜/假日缺口的市場時語意會不對齊，需要交易時段感知（session-aware）版本，否則數字只能當框架可運作的煙霧測試。
- **跨市場的情緒/總經代理變數不通用**：某市場的情緒/總經序列套到結構不同的市場、退回中性 fallback 時，濾鏡形同虛設，回測結果只測到了濾鏡以外的子集邏輯。
- **外部資料源的可交易性缺口**：離線研究能取得的額外資料，若正式引擎的執行環境無網路存取，部署後讀不到——需要引擎支援「額外欄位注入」，資料源解決了不代表能上線。

## 目錄結構

- `strategies/<name>/`：因子驗證**通過**的家族——`strategy.py`（`BaseStrategy` 子類 + CLI 進場點）、`utils.py`（特徵+訊號)、`config.yaml`（參數)、`factor_research.py`、`report.md`。目前有哪些家族在這裡，見 `FACTOR_ANALYSIS.md`。
- `strategies/experiments/<name>/`：研究中或**未通過**驗證的家族——只有 `factor_research.py`/`utils.py`/`report.md`（部分是另一個專案的舊研究，不能在本 repo 執行，見該資料夾 `README.md`）。
- `strategies/module/`：共用工具層——`data/`（資料存取,`ohlcv.py`/`factors.py`/`funding.py`/`cross_asset.py`/`regime.py` 等）、`factors/`（因子公式目錄 `library.py`、共用算子 `operators.py`、因子檢定 `utils.py`）、`utils.py`（IS/Val/OOS 切分、HTF merge)。
