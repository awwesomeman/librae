# MTF Trend RSI — 研究報告

**時間**: 2026-07-12 | **決策資產**: BTC | **跨資產**: BTC, ETH, TXFR1
**樣本切分**: IS-Train 2024-08-01~2025-04-30 | IS-Val ~2025-09-30 | OOS ~2026-06-01 | 跨資產窗口 2025-01-01~2026-06-01（同參數不重調）

流程依 `strategies/README.md` 的 ①~⑧ 順序執行。**這是本家族第一支因子研究腳本**——先前只有 `optimize_sl_tp.py`（純 SL/TP 網格搜尋），從未對 RSI(14) 或 mom_1D_10 日線趨勢濾網做過任何因子顯著性驗證，等於一支已部署策略跳過③~⑤直接做⑥，本報告補齊完整流程。

## ③-freq 基礎頻率橫掃（IS-Train，1H/4H/1D）
`trend_factor`（各基礎頻率下對應部署 10-daily-bar / 240h 窗口的動量因子）與 `rsi_demeaned`（RSI(14)-50，週期固定不隨基礎頻率變動，跟部署邏輯一致）在三個基礎頻率上橫掃。

**多重檢定校正方法**：用 `utils/stats.py` 的手刻 `holm_bonferroni`（Holm step-down，控制 **FWER**），不是 factrix 現成工具。這個網格搜尋的決策形態是「掃過 base_tf×factor×horizon 全部組合、挑單一贏家」，需要 FWER（控制「至少挑錯一次」的機率），不是 FDR（適合「篩一批因子、全部留著用」）。factrix 公開 API 目前只有 FDR 工具（`fx.stats.bhy_adjusted_p`/`fx.multi_factor.bhy`/`bhy_hierarchical`）——`factrix/_stats/multiple_testing.py` 雖有 `holm_step_down`/`bonferroni`，但那是底線開頭的 private module，未被 `factrix/__init__.py` 或公開的 `factrix/stats/` 引用，不是穩定 API，這是 README ③ 認定的真實 factrix 缺口，故此處保留手刻。

| 基礎頻率 | 因子 | Forward(bars) | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|---|
| 1h | trend_factor | 1 | 0.4982 | 0.6761 | 1.0000 |
| 1h | rsi_demeaned | 1 | 0.4942 | 0.8350 | 1.0000 |
| 1h | trend_factor | 4 | 0.4901 | 0.8603 | 1.0000 |
| 1h | rsi_demeaned | 4 | 0.4850 | 0.8959 | 1.0000 |
| 1h | trend_factor | 12 | 0.5258 | 0.1349 | 1.0000 |
| 1h | rsi_demeaned | 12 | 0.5182 | 0.2135 | 1.0000 |
| 1h | trend_factor | 24 | 0.4751 | 0.8121 | 1.0000 |
| 1h | rsi_demeaned | 24 | 0.4444 | 0.9665 | 1.0000 |
| 4h | trend_factor | 1 | 0.5035 | 0.4718 | 1.0000 |
| 4h | rsi_demeaned | 1 | 0.5067 | 0.3493 | 1.0000 |
| 4h | trend_factor | 2 | 0.4885 | 0.7750 | 1.0000 |
| 4h | rsi_demeaned | 2 | 0.5026 | 0.4690 | 1.0000 |
| 4h | trend_factor | 3 | 0.4761 | 0.8948 | 1.0000 |
| 4h | rsi_demeaned | 3 | 0.4914 | 0.6925 | 1.0000 |
| 4h | trend_factor | 6 | 0.5134 | 0.4371 | 1.0000 |
| 4h | rsi_demeaned | 6 | 0.5134 | 0.3911 | 1.0000 |
| 1d | trend_factor | 1 | 0.4864 | 0.7035 | 1.0000 |
| 1d | rsi_demeaned | 1 | 0.4942 | 0.6034 | 1.0000 |
| 1d | trend_factor | 2 | 0.4453 | 0.9151 | 1.0000 |
| 1d | rsi_demeaned | 2 | 0.4375 | 0.9374 | 1.0000 |
| 1d | trend_factor | 3 | 0.5647 | 0.1827 | 1.0000 |
| 1d | rsi_demeaned | 3 | 0.5294 | 0.3934 | 1.0000 |
| 1d | trend_factor | 5 | 0.4706 | 0.7305 | 1.0000 |
| 1d | rsi_demeaned | 5 | 0.4902 | 0.6113 | 1.0000 |

Holm-Bonferroni 校正（n=24 個檢定，跨 base_tf × factor × horizon 一起校正，alpha=0.05）後，沒有任何組合顯著。1H/4H/1D 三個基礎頻率上，trend_factor/rsi_demeaned 皆未通過校正後的顯著性門檻——這不是 1H 特有的問題，換粗/細基礎頻率沒有找到可用的邊際。

## ③ 因子分析：多頻率橫掃（IS-Train，部署邏輯的兩個實際因子）
這個 2 因子 x 4 持有期的橫掃本身也是「掃過網格挑最適持有期」的決策形態，跟③-freq 一樣需要 FWER 校正（同一套手刻 `holm_bonferroni`），不能只看 raw p 判斷顯著性——否則會跟③-freq 用不同標準評估同一份報告。

| 因子 | 持有期 | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|
| mom_1D_10 | 1h | 0.5142 | 0.0179 | 0.1788 |
| rsi_demeaned | 1h | 0.4942 | 0.8350 | 0.6599 |
| mom_1D_10 | 4h | 0.5360 | 0.0052 | 0.0630 |
| rsi_demeaned | 4h | 0.4850 | 0.8959 | 0.6248 |
| mom_1D_10 | 12h | 0.5870 | 0.0000 | 0.0007 |
| rsi_demeaned | 12h | 0.5182 | 0.2135 | 0.6599 |
| mom_1D_10 | 24h | 0.5862 | 0.0029 | 0.0407 |
| rsi_demeaned | 24h | 0.4444 | 0.9665 | 0.2682 |

Holm-Bonferroni 校正（n=8 個檢定，跨 2 因子 x 4 持有期一起校正，alpha=0.05）後：mom_1D_10 在最穩定的 12h 上 p_holm=0.0007（通過顯著性門檻）；rsi_demeaned 在最穩定的 24h 上 p_holm=0.2682（未通過顯著性門檻）。

## ③b 因子邊際穩定性（oos_decay，IS-Train 內部 70/30 切分）
| 因子 | 頻率 | 存活率 | 反號 | 狀態 |
|---|---|---|---|---|
| mom_1D_10 | 12h | 0.7172 | 否 | PASS |
| rsi_demeaned | 24h | 126.0146 | 否 | PASS |

存活率（絕對值 mean_OOS / mean_IS）< 0.5 或反號代表這個因子在 IS-Train 內部自己都不穩定，不用等到 IS-Val 就已經是警訊。

## ③c 日線趨勢 Regime 切片檢定（IS-Train，用③挑出的 RSI 頻率）
這一節直接檢驗本家族的核心設計假設——疊加日線趨勢濾網是否真的改善 RSI 的預測力：

rsi 去均值 @ 24h，依 `mom_1D_10 > 0`（多頭 regime）切片：
| Regime (是否多頭) | Hit Rate | p-value |
|---|---|---|
| false | 0.4595 | 0.8551 |
| true | 0.4333 | 0.9823 |

## ④ 頻率/持有期決定
mom_1D_10 在 12h 最穩定顯著、效應方向為「trend」（hit=0.5870, p_holm=0.0007, Holm 校正後仍顯著）；rsi_demeaned 在 24h 最穩定顯著、效應方向為「reversal」（hit=0.4444, p_holm=0.2682，Holm 校正後不顯著，符合策略假設的「RSI 超買超賣反轉」方向）。策略部署檔（mtf_trend_rsi_strategy.py）逐 1H bar 判斷進出場、以 RSI(14) 的邏輯出場（非固定 forward period），跟橫掃出來的最適 forward period 不完全是同一件事——這裡誠實記錄，不回頭改動已部署的策略頻率（見結論）。

## ⑤ 策略候選比較（IS-Val）
| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| Pure RSI (無濾網) | -31.32% | -3.0148 | -31.32% | 185 |
| MTF Trend + RSI (部署邏輯) | -2.93% | -0.2823 | -10.33% | 90 |

IS-Val 勝出候選：**MTF Trend + RSI**（MTF 濾網加值）。

## ⑤b 盲測 OOS
| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| Pure RSI (無濾網) | -35.50% | -1.4249 | -43.50% | 312 |
| MTF Trend + RSI (部署邏輯) | 23.76% | 1.3028 | -15.55% | 162 |

OOS：MTF 濾網同樣加值（與 IS-Val 結論一致）。

## ⑥ MAE/MFE SL/TP 校準（IS-Train，取代 `optimize_sl_tp.py` 原本的網格搜尋）
`optimize_sl_tp.py` 先前對 SL×TP 做 7×9=63 組合的網格搜尋，這本身是另一輪多重檢定（見 README ⑥、`quant-multiple-testing` skill）。本節改用 IS-Val 勝出候選（MTF Trend + RSI）在 IS-Train 上的進場事件，直接用 MAE/MFE 分布反推 SL/TP，不窮舉網格。

**校準結果**：SL=3.12% (P75 |MAE|) / TP=2.04% (P50 MFE)，72 個進場事件（median MAE=-1.33%, median MFE=2.04%）

Held-out（交易級交叉檢查，套用 IS-Train 校準出的固定 SL/TP，不重新校準）：
| 區間 | 無SL/TP | Sharpe | +SL/TP | Sharpe |
|---|---|---|---|---|
| IS-Val | -1.64% | -0.2677 | 3.63% | 1.0815 |
| OOS | 26.75% | 2.4333 | 10.91% | 1.3313 |

## ⑦ 正式引擎交叉驗證（BacktestService，跨資產同參數不重調）／ ⑧ 跨資產穩健性
`mtf_trend_rsi_strategy.py` 本身固定是 MTF Trend + RSI 邏輯（不論⑤的 IS-Val 挑選結果為何），跨資產穩健性驗證直接沿用這支部署檔、同一組門檻套用到 BTC/ETH/TXFR1（不重新調參）：

| 資產 | 累積報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| BTC | 61.49% | 1.03 | -18.44% | 176 |
| ETH | 90.16% | 1.01 | -30.21% | 181 |
| TXFR1 | -5.63% | -0.32 | -15.83% | 107 |

MAE/MFE SL/TP 疊加（正式引擎）：
| 資產 | 無SL/TP | Sharpe | +SL/TP | Sharpe |
|---|---|---|---|---|
| BTC | 61.49% | 1.03 | 36.52% | 0.69 |
| ETH | 90.16% | 1.01 | -6.31% | 0.01 |
| TXFR1 | -5.63% | -0.32 | -10.88% | -0.61 |

## 結論
- **本家族先前完全沒有做過因子驗證**：部署前只有 SL/TP 網格搜尋（`optimize_sl_tp.py`），從未檢驗過 RSI(14) 或 mom_1D_10 日線趨勢濾網是否真的有預測力，也從未檢驗過 MTF 濾網本身是否比不加濾網的純 RSI 反轉更好。本報告是本家族的第一次完整③~⑧驗證。
- **因子顯著性**：③（部署邏輯的 mom_1D_10/rsi_demeaned，Holm 校正 n=8）——mom_1D_10 @ 12h 通過校正後顯著性（p_holm=0.0007），rsi_demeaned @ 24h 未通過校正後顯著性（p_holm=0.2682）。③-freq（同樣因子概念、跨 1H/4H/1D 重新取資料橫掃，Holm 校正 n=24）沒有任何組合顯著。mom_1D_10 在③（用部署本身的日線聚合定義）通過校正，但③-freq 用小時線 shift(240) 逼近同一個 10-daily-bar 窗口卻沒有通過——這兩個因子在數學上不完全等價（daily-close 聚合 vs. 240 根小時 K 位移），差異本身就是一個誠實的警訊：mom_1D_10 的顯著性可能對「用日線收盤價聚合」這個具體構造方式敏感，不是一個在任何等價頻率下都穩健重現的邊際。rsi_demeaned 在兩節都未通過校正，本報告不會因為⑤/⑦回測數字好看就淡化這個結論——因子分析與回測績效是兩件事，回測正報酬可能只是特定窗口的雜訊、mom_1D_10 濾網本身的方向性 beta，或兩者疊加，不能倒推出 RSI 反轉訊號本身具備獨立、可泛化的預測力。
- **MTF 濾網是否加值**：見⑤/⑤b。IS-Val 上MTF Trend + RSI（Sharpe -0.2823）優於 Pure RSI（-3.0148）；OOS 盲測結論一致。但③c 的 regime 切片檢定顯示 RSI 在多頭（p=0.9823）與空頭（p=0.8551）兩個 regime 下的 hit rate 都遠離顯著、且彼此差異不大——RSI 本身的方向性預測力沒有因為套上日線趨勢濾網而改善，⑤/⑤b 看到的 Sharpe 提升更可能來自「濾網把交易次數砍半、只在跟日線趨勢同向時才進場」帶來的方向性 beta 曝險，而不是 RSI 訊號的品質被濾網「淨化」了。
- **頻率落差**：③橫掃出的最穩定 forward period（12h / 24h）跟部署策略的邏輯出場（RSI 打回 65/35，非固定 forward period）不是同一件事——這是已知的方法論落差，尚未回頭調整部署邏輯，如同 `mtf_trend_momentum`/`adaptive_switching` 的先例一併誠實記錄。
- **SL/TP**：⑥用 MAE/MFE 百分位法取代原本 `optimize_sl_tp.py` 的網格搜尋，held-out 與正式引擎數字見上表，結論以實際數字為準，不預設加或不加 SL/TP 一定比較好。
- **`optimize_sl_tp.py` 的處置**：其網格搜尋（1a 節）已被本報告⑥的 MAE/MFE 方法取代（更符合 README ⑥ 反網格搜尋的指引）；其 MAE/MFE 校準（1b 節）邏輯與本報告⑥實質相同，功能已完全併入本腳本。連同 `sl_tp_robustness_report.md` 一併移除，避免兩份重疊但可能隨時間漂移出不一致結論的報告並存。
