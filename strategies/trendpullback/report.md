# TrendPullback — 回溯驗證報告（H1 + M5）

**時間**: 2026-07-20~21 | **決策資產**: BTCUSDT | **跨資產**: ETHUSDT（同參數不重調，僅 H1）
**基礎頻率**: H1（gate=1D，部署預設）+ M5（gate=30min，原 `trendpullback_m5`，已合併進本資料夾，見下方「H1 vs M5」）
**H1 樣本切分**: IS-Train 2024-01-01~2024-12-31 | IS-Val ~2025-08-31 | OOS ~2026-07-01
**M5 樣本切分**: IS-Train 2025-08-01~2025-12-31 | IS-Val ~2026-03-31 | OOS ~2026-07-01（M5 bar 密度高，縮短視窗換取可行的抓取時間）
**持有期**: 24 根 bar（策略部署參數 `max_hold_periods`，H1/M5 通用，未做橫掃——這裡驗證的是已部署的頻率，不是替它找更好的頻率）

`trendpullback_m5` 原本是獨立資料夾（M5 base + M30 gate，跟 H1 版本除了時間框架完全相同：同一個 `TrendPullbackStrategy`、同一套 `prepare_signals` 邏輯）。已合併進本資料夾，`gate_timeframe` 變成 `config.yaml`/`params` 的一個欄位（H1 預設 `1D`，M5 用法設 `30min`）——見下方結論，**兩個頻率都沒過驗證，合併純粹是消除重複程式碼，不是因為找到了該用哪個的答案**。

流程依 `strategies/experiments/RESEARCH_METHODOLOGY.md` 的 ①~⑧ 順序執行，工具對應到本 repo 實際可用的版本：`strategies.data.ohlcv.get_ohlcv`（不是舊專案的 `utils/data.py`）、`librae.backtest.engine.Backtest`（不是 `BacktestService`，本 repo 沒有另外的手刻模擬器，⑦「正式引擎交叉驗證」直接就是這裡用的引擎）。表格格式沿用 `strategies/experiments/*/report.md` 既有慣例（淨報酬/Sharpe/最大回撤/交易次數 四欄；MAE/MFE 用分位數，不是平均值）。

**這是本策略第一份驗證報告**——`trendpullback` 先前直接部署（目前在跑 sim），從未做過因子顯著性檢定、樣本外驗證、或跨資產穩健性檢查，等於跳過①~⑤、⑧直接上線。本報告補做完整流程，**含一個先修的 look-ahead bug**（見下）。

## 0. 先修 bug：daily_trend gate 的同日前視偏誤

`merge_trend_gate`（`strategies/trendpullback/utils.py`）把 D1 的 `daily_trend` gate 用 `merge_asof(direction="backward")` 併回 H1。問題：`resample_ohlcv` 的 D1 index 是**當天起點**（left-labeled），若不做位移，backward-merge 會把「今天」尚未收盤的 gate 值，回填給「今天」自己所有的 H1 bar（含 00:00 那根）——今天開盤的進場決策，不可能知道今天收盤會不會站上 EMA。

**修法**：`compute_trend_bool(gate).shift(1)`，讓 H1 bar 只看得到「昨天」已完成的 gate。已加回歸測試（`tests/strategies/test_look_ahead_bias.py::test_d1_trend_change_not_visible_same_day`）——刻意還原成沒有 `shift(1)` 的版本重跑過一次，確認測試會 fail，證明測試真的在抓這個 bug，不是空判斷。

**本報告以下所有數字都是修完 bug 之後跑出來的**——修 bug 前的回測數字（原本部署時看到的績效）系統性偏高，不可信，不在這裡重現。

## 0b. 順便修的引擎效能問題（`librae/backtest/engine.py`）

驗證 M5 版本時發現 `Backtest._precompute_bars()` 用 `df.groupby(level="datetime")` 逐 group 迭代再 `to_dict()`——單一資產的資料等於拆成「每組一列」的 9 萬多個小 group，pandas groupby 的逐 group 建構開銷在這種情境下會壓垮效能。實測：97,633 列的 M5 資料，groupby 寫法只有 **490 rows/sec**（換算單一步驟就要 3.3 分鐘），改成一次性 `to_dict(orient="index")` 後 **869,136 rows/sec**（**1770 倍加速**，結果驗證完全一致）。已修正並跑過全部測試（391 個測試全過）。這不是這次因子驗證的結論範圍，但影響所有用這個引擎跑大資料量（M5、M1 等）回測的人，記錄在這裡供未來查閱。

## ③ 因子顯著性：entry_signal 有沒有方向性預測力

用 `factrix.metrics.event_quality.event_hit_rate`（Pesaran-Timmermann 二項檢定，H0: hit rate = 0.5），`entry_signal` 當作事件密度，forward return 用部署持有期（24 bars）。同時測「有 gate」（部署邏輯）跟「無 gate」（⑤ 要求的基準，`daily_trend` 強制為 True）兩個版本，三段樣本各測一次。

| 樣本 | 版本 | n_events | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|---|
| IS-Train | with_gate | 142 | 0.5000 | 1.0000 | 1.0000 |
| IS-Train | no_gate | 241 | 0.5042 | 0.8973 | 1.0000 |
| IS-Val | with_gate | 82 | 0.4756 | 0.6587 | 1.0000 |
| IS-Val | no_gate | 135 | 0.4925 | 0.8628 | 1.0000 |
| OOS | with_gate | 59 | 0.4915 | 0.8964 | 1.0000 |
| OOS | no_gate | 175 | 0.4571 | 0.2568 | 1.0000 |

Holm-Bonferroni 校正（n=6 個檢定，跨樣本×版本一起校正，alpha=0.05，FWER——這是「比較兩個版本挑一個」的決策形態，不是留著全部用的 FDR 情境）後：**沒有任何一格顯著**。原始 p 值本身就全部 > 0.25，Hit Rate 全部落在 0.46~0.50 之間，統計上跟丟硬幣沒有差異。`entry_signal`（不管有沒有 daily_trend gate）在 24 bar 的持有期上，看不出任何方向性預測力。

## ⑤ 策略候選比較 + ⑦ 正式引擎回測（IS-Train+IS-Val，零成本）

`CostModel.zero()`——以下數字**不含手續費/滑價/稅**，是策略邏輯本身的上限，不是可交易的淨值。

| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| with_gate | +5.98% | 0.296 | -14.00% | 183 |
| no_gate | -6.02% | -0.059 | -16.98% | 294 |

樣本內看起來 gate 有幫助（Sharpe 由負轉正、報酬由負轉正）——但見下方 ⑦ OOS 跟 ⑧ 跨資產，這個排序不會延續。

## ⑥ MAE/MFE 分布（with_gate，IS-Train 進場事件）

115 個進場事件：median MAE = -0.80% / median MFE = 0.70% / P75 |MAE| = 1.27%。

若要疊加固定 SL/TP（目前策略沒有——出場邏輯是 EMA 破位 + 最大持有期），校準候選會是 **SL=1.27%（P75 |MAE|）/ TP=0.70%（P50 MFE）**。這裡只記錄分布供參考，**不套用**——見下方「已知限制」。

## ⑦ OOS 盲測（同一組參數，完全沒被用來挑選過任何東西）

| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| with_gate | -7.76% | -0.907 | -11.07% | 47 |
| no_gate | -3.29% | -0.120 | -11.05% | 138 |

**關鍵發現，且違反原始設計假設**：樣本內看起來比較好的 `with_gate`，OOS 是兩個版本裡表現最差的（Sharpe -0.91，淨報酬 -7.76%）。`no_gate` 雖然也賠錢，但賠得少很多。這是典型的樣本內過擬合信號——daily_trend 濾網在 IS 上看起來有加值，但換到沒看過的資料就不成立，甚至更糟。

## ⑧ 跨資產穩健性（ETHUSDT，同參數，不重調）

| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| with_gate | +9.71% | 0.291 | -16.13% | 184 |
| no_gate | +44.36% | 0.652 | -28.35% | 434 |

同樣的模式：`no_gate` 在 ETH 上明顯優於 `with_gate`（Sharpe 0.65 vs 0.29，淨報酬 +44.4% vs +9.7%，雖然最大回撤也更大）。跟 OOS 的結論一致——`daily_trend` 這個濾網不是在過濾出更好的進場，比較像是在過濾掉獲利交易。

## H1 vs M5 比較

M5（base=M5, gate=30min）跑同一套 ③ 因子檢定 + ⑦ 回測：

| 樣本 | 版本 | n_events | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|---|
| IS-Train | with_gate | 501 | 0.4551 | 0.0444 | 0.1775 |
| IS-Train | no_gate | 937 | 0.4867 | 0.4141 | 0.4141 |
| IS-Val | with_gate | 283 | 0.4433 | 0.0567 | 0.1775 |
| IS-Val | no_gate | 584 | 0.4597 | 0.0516 | 0.1775 |
| OOS | with_gate | 282 | 0.3830 | 0.0001 | **0.0005（PASS）** |
| OOS | no_gate | 542 | 0.4463 | 0.0126 | 0.0628 |

**唯一一個 Holm 校正後顯著的格子（OOS/with_gate）,hit rate=0.383——顯著低於 0.5，是「顯著地往反方向預測」，不是找到真邊際**：進場訊號觸發後，價格往下走的機率統計上顯著高於往上，方向完全跟策略假設（做多）相反。

回測（零成本）：

| 樣本 | 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|---|
| IS-Train+Val | with_gate | +5.79% | 0.679 | -10.03% | 643 |
| IS-Train+Val | no_gate | +2.15% | 0.262 | -16.77% | 1239 |
| OOS | with_gate | -8.19% | -3.027 | -10.95% | 235 |
| OOS | no_gate | -9.85% | -2.346 | -13.61% | 454 |

**M5 的 OOS 比 H1 的 OOS 更差**（Sharpe -3.03 vs H1 的 -0.91），不是「訓練/測試都穩健的版本」——H1、M5 兩個頻率都沒過驗證，M5 甚至更糟，不是次優選項。

## 結論

1. **③ 因子顯著性未過關（H1 全部、M5 幾乎全部）**：`entry_signal` 在 H1 任何樣本、有無 gate，Holm 校正後都不顯著。M5 唯一顯著的一格（OOS/with_gate）是方向相反的顯著（hit rate 0.383），不是可用的邊際——沒有任何一個頻率、任何一個版本，統計上站得住腳。
2. **`daily_trend`/HTF gate 濾網在兩個頻率上都跟原始設計假設相反**：H1 樣本內看似加值，OOS 與跨資產（ETH）都顯示它讓表現變差；M5 同樣模式，且 OOS 更差。過擬合訊號，不是真實的濾網效果，兩個頻率一致。
3. **以上全部是零成本數字**——加上真實手續費/滑價（Binance spot ~0.1%/邊 + 滑價），H1 183~434 筆、M5 235~1239 筆交易的成本會顯著吃掉本來就很薄的邊際（IS 淨報酬本身只有個位數百分比、OOS 已經是負的），真實淨值大概率更差，M5 交易頻率更高、成本侵蝕會更嚴重。
4. **MAE 中位數(-0.80%) 比 MFE 中位數(0.70%) 幅度更大**（H1 數字），方向上不利：平均逆行比平均順行還深，這跟一個「有效」的濾網應該篩出的樣貌相反。

**建議：不建議維持現狀繼續跑 live/sim，H1、M5 都不建議。** 具體選項：
- 拿掉 `daily_trend` 濾網（`no_gate` 版本在兩個頻率、OOS/跨資產都比較不差），但即使拿掉，OOS 依然是負報酬，不是「拿掉濾網就沒事」。
- 回到③重新找有顯著方向性的因子，再往下走④~⑧，而不是繼續在一個已經測不出顯著性的訊號上調參數或換頻率。
- 若要保留現有邏輯当作 baseline/煙霧測試用途，至少要把「這是未過因子顯著性檢定、H1/M5 OOS 皆為負」的事實留在文件裡，不要讓後續開發誤以為這是一個已驗證的策略。
- `trendpullback_m5` 已合併進本資料夾（`gate_timeframe` 參數），不用再維護兩份重複程式碼；但這不代表任一頻率「過關」了。

## 已知限制

- 只驗證了單一資產家族（BTC 決策、ETH 穩健性），是本方法論①要求的最低配置，不是廣泛掃過的資產池。
- 沒有做④的頻率/持有期橫掃——24 bar 是部署參數，這裡驗證的是「這個參數下有沒有效」，不是「換個參數會不會更好」；若要回答後者需要另外橫掃（本身又是一輪多重檢定，需要另外的 FWER 校正）。
- ⑥ 的 MAE/MFE 分布只算了 `with_gate` 在 IS-Train 的診斷，沒有真的疊加 SL/TP 回測驗證——因為③已經顯示這個進場訊號本身沒有顯著方向性，在一個測不出邊際的訊號上校準停損停利沒有意義，先解決③再回頭做這一步。
