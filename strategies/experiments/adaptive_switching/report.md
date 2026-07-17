# Adaptive Switching — 研究報告

**時間**: 2026-07-12 | **決策資產**: BTC | **跨資產**: BTC, ETH, TXFR1
**樣本切分**: IS-Train 2024-08-01~2025-04-30 | IS-Val ~2025-09-30 | OOS ~2026-06-01 | 跨資產窗口 2025-01-01~2026-06-01（同參數不重調）

流程依 `strategies/README.md` 的 ①~⑧ 順序執行。

## Bug（已修復）
`vol_ratio` 的 pivot 計算在 TXFR1 上因重複 `(date,hour)` 列崩潰 → 改用 `groupby().last().unstack()`，已修復並在⑦驗證不再崩潰。

## ③-freq 基礎頻率橫掃（IS-Train，1H/4H/1D）
mom_factor/rsi_demeaned 在 1H 上所有 forward period 都不顯著（見下方③），依 README ③ 換基礎頻率重新取資料橫掃，而非死守 1H。mom_factor lookback 隨基礎頻率調整（1H: 12 bars / 4H: 3 bars / 1D: 1 bar，皆對應約 12h 窗口，1D 上收斂為 1 bar 因日線沒有次日內窗口）；forward period 以 bar 數表示。

**多重檢定校正方法**：用 `utils/stats.py` 的手刻 `holm_bonferroni`（Holm step-down，控制 **FWER**），不是 factrix 現成工具。原因：這個網格搜尋的決策形態是「掃過 base_tf×factor×horizon 全部組合、挑單一贏家」，需要的是 FWER（控制「至少挑錯一次」的機率），不是 FDR（控制「誤判佔比的期望值」，適合「篩一批因子、全部留著用」的情境，不適合「只挑最強的那一個」）。**factrix 公開 API 目前只有 FDR 工具**（`fx.stats.bhy_adjusted_p`/`fx.multi_factor.bhy`/`bhy_hierarchical`）——`factrix/_stats/multiple_testing.py` 雖然有 `holm_step_down`/`bonferroni`，但那是底線開頭的 private module，未被 `factrix/__init__.py` 或公開的 `factrix/stats/` 引用，不能當作穩定 API 依賴。這是 README ③ 認定的真實 factrix 缺口，故此處保留手刻，不換成 `bhy_adjusted_p`（先前一度換過，回頭發現那其實是換錯了統計目標，已改回）。

| 基礎頻率 | 因子 | Forward(bars) | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|---|
| 1h | mom_factor | 1 | 0.4886 | 0.9717 | 1.0000 |
| 1h | rsi_demeaned | 1 | 0.4952 | 0.7987 | 1.0000 |
| 1h | mom_factor | 4 | 0.4754 | 0.9807 | 0.8867 |
| 1h | rsi_demeaned | 4 | 0.4724 | 0.9900 | 0.4784 |
| 1h | mom_factor | 12 | 0.4834 | 0.7826 | 1.0000 |
| 1h | rsi_demeaned | 12 | 0.4649 | 0.9509 | 1.0000 |
| 1h | mom_factor | 24 | 0.4649 | 0.8735 | 1.0000 |
| 1h | rsi_demeaned | 24 | 0.4834 | 0.7446 | 1.0000 |
| 4h | mom_factor | 1 | 0.4842 | 0.9145 | 1.0000 |
| 4h | rsi_demeaned | 1 | 0.5071 | 0.3360 | 1.0000 |
| 4h | mom_factor | 2 | 0.4882 | 0.7649 | 1.0000 |
| 4h | rsi_demeaned | 2 | 0.5068 | 0.3707 | 1.0000 |
| 4h | mom_factor | 3 | 0.4694 | 0.9301 | 1.0000 |
| 4h | rsi_demeaned | 3 | 0.5065 | 0.3934 | 1.0000 |
| 4h | mom_factor | 6 | 0.5019 | 0.5633 | 1.0000 |
| 4h | rsi_demeaned | 6 | 0.5279 | 0.2439 | 1.0000 |
| 1d | mom_factor | 1 | 0.5136 | 0.3394 | 1.0000 |
| 1d | rsi_demeaned | 1 | 0.4942 | 0.6034 | 1.0000 |
| 1d | mom_factor | 2 | 0.4297 | 0.9686 | 1.0000 |
| 1d | rsi_demeaned | 2 | 0.4375 | 0.9374 | 1.0000 |
| 1d | mom_factor | 3 | 0.4824 | 0.6392 | 1.0000 |
| 1d | rsi_demeaned | 3 | 0.5294 | 0.3934 | 1.0000 |
| 1d | mom_factor | 5 | 0.5294 | 0.2985 | 1.0000 |
| 1d | rsi_demeaned | 5 | 0.4902 | 0.6113 | 1.0000 |

Holm-Bonferroni 校正（n=24 個檢定，跨 base_tf × factor × horizon 一起校正，alpha=0.05）後，沒有任何組合顯著。1H/4H/1D 三個基礎頻率上，mom/rsi 皆未通過校正後的顯著性門檻——這不是 1H 特有的問題，換粗/細基礎頻率沒有找到可用的邊際。

## ③ 因子分析：多頻率橫掃（IS-Train）
| 因子 | 持有期 | Hit Rate | p-value |
|---|---|---|---|
| mom_1H_12 | 1h | 0.4882 | 0.9734 |
| rsi_demeaned | 1h | 0.4942 | 0.8350 |
| mom_1H_12 | 4h | 0.4927 | 0.7444 |
| rsi_demeaned | 4h | 0.4850 | 0.8959 |
| mom_1H_12 | 12h | 0.5029 | 0.4631 |
| rsi_demeaned | 12h | 0.5182 | 0.2135 |
| mom_1H_12 | 24h | 0.4483 | 0.9573 |
| rsi_demeaned | 24h | 0.4444 | 0.9665 |

## ③b 因子邊際穩定性（oos_decay，IS-Train 內部 70/30 切分）
| 因子 | 頻率 | 存活率 | 反號 | 狀態 |
|---|---|---|---|---|
| mom_1H_12 | 1h | 1.2543 | 是 | VETOED |
| rsi_demeaned | 24h | 126.0146 | 否 | PASS |

存活率（絕對值 mean_OOS / mean_IS）< 0.5 或反號代表這個因子在 IS-Train 內部自己都不穩定，不用等到 IS-Val 就已經是警訊。mom_1H_12 反號（VETOED）就是這種警訊。rsi_demeaned 存活率 126 這種遠大於 1 的數字通常是 mean_IS 接近 0 造成的分母效應，不代表真的穩定 126 倍，解讀時不應直接當作「非常穩健」的證據。

## ③c Vol-ratio Regime 切片檢定（IS-Train，用③挑出的頻率）
mom_1H_12 @ 1h：
| Regime | Hit Rate | p-value |
|---|---|---|
| false | 0.4910 | 0.8920 |
| true | 0.4808 | 0.9544 |

rsi 去均值 @ 24h：
| Regime | Hit Rate | p-value |
|---|---|---|
| false | 0.4497 | 0.9152 |
| true | 0.5068 | 0.5610 |

## ④ 頻率/持有期決定
mom_1H_12 在 1h 最穩定顯著、但效應方向是「reversal」（hit rate 0.4882 < 0.5，p=0.9734 接近 1，跟策略假設的「動量突破」方向相反）；rsi_demeaned 在 24h 最穩定顯著，方向為「reversal」。策略部署檔（adaptive_switching_strategy.py）目前逐 1H bar 判斷進出場，跟橫掃出來的最適頻率不完全一致——這裡誠實記錄落差，不回頭改動已部署的策略頻率（見結論）。

## ⑤ 策略候選比較（IS-Val）
| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| Momentum-only | -15.58% | -1.4952 | -20.83% | 226 |
| RSI-only | -2.93% | -0.2823 | -10.33% | 90 |
| Adaptive switching | -18.00% | -1.9462 | -21.62% | 190 |

## ⑤b 盲測 OOS
| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| Momentum-only | -19.64% | -0.7361 | -28.82% | 423 |
| RSI-only | 23.76% | 1.3028 | -15.55% | 162 |
| Adaptive switching | -15.72% | -0.5911 | -33.81% | 350 |

## ⑥ MAE/MFE SL/TP 校準（IS-Train）
**校準結果**：SL=2.88% / TP=2.27%（156 個進場事件）

Held-out（交易級交叉檢查，套用 IS-Train 校準出的固定 SL/TP，不重新校準）：
| 區間 | 無SL/TP | Sharpe | +SL/TP | Sharpe |
|---|---|---|---|---|
| IS-Val | -14.93% | -2.0016 | -13.55% | -1.9196 |
| OOS | -13.24% | -0.4825 | -20.93% | -1.2347 |

## ⑦ 正式引擎交叉驗證（BacktestService，跨資產同參數不重調）
| 資產 | 累積報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| BTC | -20.55% | -0.37 | -36.31% | 371 |
| ETH | 189.41% | 1.43 | -21.1% | 406 |
| TXFR1 | -14.62% | -0.76 | -24.73% | 182 |

MAE/MFE SL/TP 疊加（正式引擎）：
| 資產 | 無SL/TP | Sharpe | +SL/TP | Sharpe |
|---|---|---|---|---|
| BTC | -20.55% | -0.37 | -34.46% | -0.73 |
| ETH | 189.41% | 1.43 | -5.48% | 0.08 |
| TXFR1 | -14.62% | -0.76 | -21.56% | -1.26 |

## 結論
- **切換機制是否加值**：見⑤/⑤b。IS-Val 上 Adaptive switching（Sharpe -1.9462）是三者中最差的，並未優於兩個單一子策略中較好的一個（RSI-only -0.2823 / Momentum-only -1.4952）；OOS 盲測同樣顯示切換機制沒有加值（Adaptive switching -0.5911 vs RSI-only 1.3028 / Momentum-only -0.7361）——**兩個獨立窗口（IS-Val、OOS）都指向同一個結論，比只看單一 OOS 窗口更站得住腳**：切換機制沒有加值，且部分窗口下反而是三者中最差的。
- **mom_1H_12 的方向跟策略假設相反**：③顯示 mom_1H_12 最穩定顯著是在 1h、效應方向是「reversal」（hit rate 0.4882<0.5），但策略把它當「動量突破」訊號使用（正值視為看漲延續）——因子分析結果本身就不支持這個因子的使用方式，這比切換門檻的問題更根本。且 mom_1H_12 的 oos_decay 反號（③b，VETOED），代表這個「顯著」在 IS-Train 內部就不穩定，不需要等到 IS-Val 才發現問題。
- **頻率落差**：③橫掃出的最穩定頻率（1h/24h）跟部署策略固定逐 1H bar 判斷（見④）不完全一致——這是已知的方法論落差，尚未回頭調整部署頻率。
- ③c 的 regime 切片檢定顯示兩個因子在 `is_trend_regime` 兩側的 hit rate/p-value 都沒有達到常見顯著門檻，vol_ratio 1.15 這個切換門檻缺乏資料支持，與原始版本的結論一致。
- TXFR1 的 pivot 崩潰已修復並在⑦驗證正常運作；`vol_ratio` 假設 24/7 市場，TXFR1（日盤限定）語意仍不對齊，數字僅供框架驗證參考。
- MAE/MFE SL/TP 疊加需依⑦數字判斷是否加值，不假設對所有家族都有害——見上表。
- **基礎頻率橫掃（③-freq）**：1H 上的不顯著不是頻率選錯——換 4H/1D 重新取資料橫掃、跨 base_tf×factor×horizon 做 Holm-Bonferroni 校正（手刻，FWER control——這個「掃網格挑單一贏家」的決策形態需要 FWER，而 factrix 公開 API 沒有 FWER 工具，見③-freq 說明）後，三個基礎頻率上 mom/rsi 都沒有通過顯著性門檻，代表 mom/rsi 這兩個因子在 BTC 上（至少在這個樣本窗口）本身就不具備可用邊際，不是 1H 這個起始假設的問題。既然三個已測基礎頻率都沒有找到顯著因子，依 README ④「若因子在所有已嘗試的頻率下都不穩定，代表它可能不夠格進入⑤」，本次不再嘗試多頻率合成（multi-timeframe combination）——在單一頻率邊際都不存在的前提下疊加 MTF 只會增加多重檢定風險，不會製造出原本沒有的邊際。目前部署的 1H adaptive switching 邏輯（④~⑧ 沿用既有結果）維持原結論：不建議上線。
