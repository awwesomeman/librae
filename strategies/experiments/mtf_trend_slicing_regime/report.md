# MTF Trend Slicing Regime — 研究報告

**時間**: 2026-07-12 | **決策資產**: BTC | **跨資產**: BTC, ETH, TXFR1
**樣本切分**: IS-Train 2024-08-01~2025-04-30 | IS-Val ~2025-09-30 | OOS ~2026-06-01 | 跨資產窗口 2025-01-01~2026-06-01（同參數不重調）

流程依 `strategies/README.md` 的 ①~⑧ 順序執行。

## 研究設計備註
* `fng_regime`（Fear & Greed）、`dxy_trend`（美元指數）是**加密貨幣市場層級**的總經/情緒序列，只對 Crypto 資產（BTC/ETH 共用同一份全球序列）有意義；套到 TXFR1 上沒有對應概念，`utils/regime.py` 對非 Crypto 資產直接給中性預設值（fng=50/"weak_dxy"），讓過濾器形同虛設但策略程式碼不用為 TXFR1 另外分支——這跟 `# @strategy` 檔案本身「欄位不存在就退回中性值」的容錯設計一致。
* `vol_regime`（波動 regime）是從**資產自己的 OHLCV** 算出來的（ATR ratio vs 自身 rolling baseline），所以是真正資產無關的 regime，可以跨資產比較。

## ③-freq 基礎頻率橫掃（IS-Train，1H/4H/1D）
mom_factor/rsi_demeaned 在 1H 上所有 forward period 都不顯著（見下方③），依 README ③ 換基礎頻率重新取資料橫掃，而非死守 1H。mom_factor lookback 隨基礎頻率調整（1H: 12 bars / 4H: 3 bars / 1D: 1 bar，皆對應約 12h 窗口，1D 上收斂為 1 bar 因日線沒有次日內窗口）；forward period 以 bar 數表示。

**多重檢定校正方法**：用 `utils/stats.py` 的手刻 `holm_bonferroni`（Holm step-down，控制 **FWER**），不是 factrix 現成工具。原因：這個網格搜尋的決策形態是「掃過 base_tf×factor×horizon 全部組合、挑單一贏家」，需要的是 FWER（控制「至少挑錯一次」的機率），不是 FDR（控制「誤判佔比的期望值」，適合「篩一批因子、全部留著用」的情境，不適合「只挑最強的那一個」）。**factrix 公開 API 目前只有 FDR 工具**（`fx.stats.bhy_adjusted_p`/`fx.multi_factor.bhy`/`bhy_hierarchical`）——`factrix/_stats/multiple_testing.py` 雖然有 `holm_step_down`/`bonferroni`，但那是底線開頭的 private module，未被 `factrix/__init__.py` 或公開的 `factrix/stats/` 引用，不能當作穩定 API 依賴。這是 README ③ 認定的真實 factrix 缺口，故此處保留手刻。

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
| mom_1H_12 | 1h | 0.4927 | 0.8692 |
| rsi_1H_14 | 1h | 0.4965 | 0.7323 |
| mom_1H_12 | 4h | 0.5043 | 0.4591 |
| rsi_1H_14 | 4h | 0.4966 | 0.6544 |
| mom_1H_12 | 12h | 0.5188 | 0.2754 |
| rsi_1H_14 | 12h | 0.5188 | 0.2713 |
| mom_1H_12 | 24h | 0.4535 | 0.8924 |
| rsi_1H_14 | 24h | 0.4593 | 0.8604 |

## ③b 因子邊際穩定性（oos_decay，IS-Train 內部 70/30 切分）
| 因子 | 頻率 | 存活率 | 反號 | 狀態 |
|---|---|---|---|---|
| mom_1H_12 | 24h | 10.1797 | 否 | PASS |
| rsi_1H_14 | 24h | 6.4313 | 是 | VETOED |

存活率（絕對值 mean_OOS / mean_IS）< 0.5 或反號代表這個因子在 IS-Train 內部自己都不穩定，不用等到 IS-Val 就已經是警訊。

## ③c Regime 切片檢定（IS-Train，用③挑出的頻率，factrix by_slice + compare）
這裡刻意**不用**正式的 `slice_pairwise_test`/`slice_joint_test`：`directional_hit_rate` 是單一資產的 TS_ONLY 指標（在自己的時間軸上算命中率），不是 `ic()`/`fm_beta()`/`positive_rate()` 這類需要「同一天有多個資產」才能算的橫截面指標，`slice_pairwise_test` 結構上就不適用。`by_slice`+`compare` 產出的是描述性排行榜，用來看「這個因子在哪個 regime 下比較有效」，不是嚴謹的假設檢定。

rsi_1H_14 @ 24h 依 Fear & Greed regime：
| Regime | Hit Rate | p-value |
|---|---|---|
| fear | 0.4118 | 0.8755 |
| greed | 0.4793 | 0.6729 |

rsi_1H_14 @ 24h 依 DXY regime：
| Regime | Hit Rate | p-value |
|---|---|---|
| weak_dxy | 0.3830 | 0.9889 |
| strong_dxy | 0.5513 | 0.1981 |

mom_1H_12 @ 24h 依波動 regime：
| Regime | Hit Rate | p-value |
|---|---|---|
| low_vol | 0.4769 | 0.7465 |
| high_vol | 0.4630 | 0.7656 |

## ④ 頻率/持有期決定
rsi_1H_14 在 24h 最穩定顯著、效應方向為「reversal」（hit rate 0.4593, p=0.8604）；mom_1H_12 在 24h 最穩定顯著、效應方向為「reversal」（hit rate 0.4535, p=0.8924）。策略部署檔（mtf_trend_slicing_regime_strategy.py）目前逐 1H bar 判斷進出場，跟橫掃出來的最適頻率不完全一致——這裡誠實記錄落差，不回頭改動已部署的策略頻率（見結論）。

## ⑤ 策略候選比較（IS-Val）
| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| No regime filter (baseline) | -2.37% | -0.2241 | -13.37% | 67 |
| Regime filter (deployed logic) | -4.50% | -0.6116 | -10.23% | 55 |

## ⑤b 盲測 OOS
| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| No regime filter (baseline) | 37.61% | 2.3203 | -14.68% | 116 |
| Regime filter (deployed logic) | 54.01% | 3.5176 | -11.81% | 83 |

## ⑥ MAE/MFE SL/TP 校準（IS-Train）
**校準結果**：SL=4.77% / TP=2.35%（35 個進場事件）

Held-out（交易級交叉檢查，套用 IS-Train 校準出的固定 SL/TP，不重新校準）：
| 區間 | 無SL/TP | Sharpe | +SL/TP | Sharpe |
|---|---|---|---|---|
| IS-Val | -6.77% | -2.1076 | -6.05% | -1.9441 |
| OOS | 54.06% | 7.7018 | 35.09% | 6.3667 |

## ⑦ 正式引擎交叉驗證（BacktestService，跨資產同參數不重調）
| 資產 | 累積報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| BTC | 61.49% | 1.03 | -18.44% | 176 |
| ETH | 90.16% | 1.01 | -30.21% | 181 |
| TXFR1 | -5.63% | -0.32 | -15.83% | 107 |

MAE/MFE SL/TP 疊加（正式引擎）：
| 資產 | 無SL/TP | Sharpe | +SL/TP | Sharpe |
|---|---|---|---|---|
| BTC | 61.49% | 1.03 | 43.2% | 0.79 |
| ETH | 90.16% | 1.01 | 17.22% | 0.34 |
| TXFR1 | -5.63% | -0.32 | -0.0% | -0.03 |

## ⑧ 跨資產穩健性（`asset_id × vol_regime` 複合切片，同參數不重調）
`vol_regime` 是唯一一個三個資產都能公平比較的 regime（不像 fng/dxy 只對 Crypto 有意義）。用 `pl.concat_str([asset_id, vol_regime])` 組合鍵餵給 `by_slice`，一次看完「每個資產、每個波動 regime」下 `mom_1H_12` 的命中率，並對這 6 個切片做 Holm 校正（同一因子跨多個切片測，等同 K=6 的多重檢定，同③-freq 的 FWER 理由）：

| 資產_regime | Hit Rate | p-value | Holm 校正後 p-value |
|---|---|---|---|
| BTC_low_vol | 0.5372 | 0.2069 | 1.0000 |
| BTC_high_vol | 0.4570 | 0.8887 | 1.0000 |
| ETH_low_vol | 0.4191 | 0.9589 | 1.0000 |
| ETH_high_vol | 0.4466 | 0.9378 | 1.0000 |
| TXFR1_low_vol | 0.9000 | 0.0131 | 0.0788 |
| TXFR1_high_vol | N/A | 1.0000 | 1.0000 |

## 結論
- **regime 濾鏡是否加值**：見⑤/⑤b。IS-Val 上 Regime filter（Sharpe -0.6116）並未優於 No-filter baseline（-0.2241）；OOS 盲測反過來顯示 Regime filter 優於 baseline（3.5176 vs 2.3203）——兩個窗口的排序不一致，代表濾鏡的加值（或減損）本身也不穩定，不能只憑其中一個窗口下結論。
- **因子顯著性**：③顯示 mom_1H_12/rsi_1H_14 在 IS-Train 上多頻率橫掃的顯著性與方向見上表；③b 的 oos_decay 進一步檢查這個邊際在 IS-Train 內部是否穩定（反號或存活率過低即為警訊，見③b 表格與註記）。
- **頻率落差**：③橫掃出的最穩定頻率（rsi 24h / mom 24h）跟部署策略固定逐 1H bar 判斷（見④）之間的一致性見④——這是已知的方法論落差，尚未回頭調整部署頻率。
- ③c 的 regime 切片檢定顯示 rsi_1H_14/mom_1H_12 在 fng/dxy/vol_regime 各分片下的 hit rate/p-value 是否達到常見顯著門檻，見上表；這是描述性排行榜，不是正式假設檢定（理由見③c 說明）。
- ⑧ 的複合切片校正後，若某個 asset_id × vol_regime 切片只在 BTC 上顯著、換資產就消失，代表那是決策資產特有的雜訊，不是可泛化的市場結構（見上表）。
- TXFR1 的 fng/dxy 過濾器恆為中性（見研究設計備註），所以 TXFR1 的回測結果本質上只測了「日線趨勢 + RSI 反彈」這個子集邏輯，不是完整的三重防禦策略；若要讓 TXFR1 的比較公平，需要幫台指期找一個有意義的情緒/總經代理變數，而不是直接沿用比特幣的 FNG/DXY。
- **基礎頻率橫掃（③-freq）**：1H 上的不顯著不是頻率選錯——換 4H/1D 重新取資料橫掃、跨 base_tf×factor×horizon 做 Holm-Bonferroni 校正（手刻，FWER control，見③-freq 說明）後，三個基礎頻率上 mom/rsi 都沒有通過顯著性門檻，代表 mom/rsi 這兩個因子在 BTC 上（至少在這個樣本窗口）本身就不具備可用邊際，不是 1H 這個起始假設的問題。
- MAE/MFE SL/TP 疊加需依⑥/⑦數字判斷是否加值，不假設對所有家族都有害或都有益——見上表。
- ⑧複合切片中 TXFR1_high_vol 的 metric 為 N/A（樣本內該切片沒有足夠的 (date, asset) 配對可算 hit rate），已在表中如實標記，不當作 0 或省略。TXFR1_low_vol 的原始 p-value（0.0131）看似顯著，但 Holm 校正後（p=0.0788）已不通過 alpha=0.05 門檻，且樣本數本來就小（見框架已知陷阱：低頻/稀疏交易市場的統計量不可靠），不構成可部署的證據。
- **整體結論**：③/③-freq 顯示 mom_1H_12、rsi_1H_14 這兩個因子在 IS-Train 上，不論 1H 起始頻率還是換到 4H/1D 重新橫掃，Holm-Bonferroni 校正後都沒有一個 (base_tf, factor, horizon) 組合顯著；③b 的 oos_decay 對 rsi_1H_14 更是 VETOED（IS-Train 內部就反號）。在核心因子本身未通過顯著性檢驗的前提下，⑤/⑤b regime 濾鏡在 IS-Val/OOS 兩個窗口的排序又互相矛盾，OOS 上看到的高 Sharpe（3.5176）更可能反映的是這段窗口本身的單邊下跌行情（market=-35.84%）被日線趨勢濾鏡+做空邏輯順勢捕捉到，而不是 rsi_1H_14/mom_1H_12 這兩個進出場時機因子本身具備統計上顯著、可泛化的邊際。依 README ④「若因子在所有已嘗試的頻率下都不穩定，代表它可能不夠格進入⑤」，這個策略族現有的因子基礎不足以支持自信部署；⑦/⑧的正式引擎數字可作為框架可運作的煙霧測試參考，但不建議僅憑本次結果上線。
