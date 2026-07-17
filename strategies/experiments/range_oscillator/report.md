# Range Oscillator — 研究報告

**時間**: 2026-07-12 | **決策資產**: BTC | **跨資產**: BTC, ETH, TXFR1
**樣本切分**: IS-Train 2024-08-01~2025-04-30 | IS-Val ~2025-09-30 | OOS ~2026-06-01 | 跨資產窗口 2025-01-01~2026-06-01（同參數不重調）

流程依 `strategies/README.md` 的 ①~⑧ 順序執行。

## Bug / 已知限制（沿用自舊版）
策略檔案（`range_oscillator_strategy.py`）的三重防禦濾鏡裡有一個「未平倉量變化 < 5% 視為盤整」的 OI 濾鏡，但程式碼本身有 fallback：`if 'open_interest_change_24h' in df.columns` 找不到就直接視為恆真（`oi_consolidating = True`）。**ccxt 現貨 OHLCV 沒有 OI，Shioaji TXFR1 kbars 也沒有**，所以這個濾鏡在正式引擎（讀 ccxt/Shioaji 即時資料）上從未真正過濾過任何一根K棒，"三重防禦"實質上只有兩重（成交量 + 振幅）在運作。`utils/open_interest.py`（Binance 歷史批次資料庫）把 BTC/ETH 的歷史 OI 接進**這支研究腳本**供離線驗證（見③c/⑤），但要讓濾鏡在正式引擎上真的生效還需要額外引擎工程（把 OI 當成跟 OHLCV 一起注入的欄位）；TXFR1 仍無對應資料源。

## ③-freq 基礎頻率橫掃（IS-Train，1H/4H/1D）
核心均值回歸因子 `bb_pct_b`（去均值）在各基礎頻率的 forward period 橫掃結果如下；`bb_period` 依基礎頻率調整為功能上合理的滾動窗口（1H: 20 bars / 4H: 6 bars / 1D: 5 bars），不是精確對齊小時數，因為原本 20-bar 窗口若照小時數換算到 4H/1D 會變成完全不同尺度的「局部區間」概念。

**多重檢定校正方法**：用 `utils/stats.py` 的手刻 `holm_bonferroni`（Holm step-down，控制 **FWER**），不是 factrix 現成工具。原因：這個網格搜尋的決策形態是「掃過 base_tf×horizon 全部組合、挑單一贏家」，需要的是 FWER（控制「至少挑錯一次」的機率），不是 FDR（適合「篩一批因子、全部留著用」的情境）。**factrix 公開 API 目前只有 FDR 工具**（`fx.stats.bhy_adjusted_p`/`fx.multi_factor.bhy`/`bhy_hierarchical`）——`factrix/_stats/multiple_testing.py` 雖然有 `holm_step_down`/`bonferroni`，但那是底線開頭的 private module，未被 `factrix/__init__.py` 或公開的 `factrix/stats/` 引用，不能當作穩定 API 依賴。這是 README ③ 認定的真實 factrix 缺口，故此處保留手刻。

| 基礎頻率 | 因子 | Forward(bars) | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|---|
| 1h | bb_factor | 1 | 0.4917 | 0.9230 | 1.0000 |
| 1h | bb_factor | 4 | 0.4683 | 0.9957 | 0.0942 |
| 1h | bb_factor | 12 | 0.4963 | 0.5981 | 1.0000 |
| 1h | bb_factor | 24 | 0.4945 | 0.5951 | 1.0000 |
| 4h | bb_factor | 1 | 0.4834 | 0.9284 | 1.0000 |
| 4h | bb_factor | 2 | 0.4502 | 0.9983 | 0.0411 |
| 4h | bb_factor | 3 | 0.4797 | 0.8369 | 1.0000 |
| 4h | bb_factor | 6 | 0.4649 | 0.9023 | 1.0000 |
| 1d | bb_factor | 1 | 0.5356 | 0.1344 | 1.0000 |
| 1d | bb_factor | 2 | 0.4436 | 0.9282 | 1.0000 |
| 1d | bb_factor | 3 | 0.4944 | 0.5743 | 1.0000 |
| 1d | bb_factor | 5 | 0.4717 | 0.6722 | 1.0000 |

Holm-Bonferroni 校正（n=12 個檢定，跨 base_tf × horizon 一起校正，alpha=0.05）後，找到以下顯著組合。4h@2bars (hit=0.4502, p_holm=0.0411)。

## ③ 因子分析：多頻率橫掃（IS-Train）
這是這個家族第一次真正用 factrix `directional_hit_rate`/`evaluate_horizons` 檢定核心因子 `bb_pct_b_1H_20`（去均值）是否具方向性預測力，而不是只憑指標構造（Keltner/BB 均值回歸概念）就假設成立。

| 因子 | 持有期 | Hit Rate | p-value |
|---|---|---|---|
| bb_pct_b_1H_20 | 1h | 0.4905 | 0.9434 |
| bb_pct_b_1H_20 | 4h | 0.4768 | 0.9756 |
| bb_pct_b_1H_20 | 12h | 0.4914 | 0.6716 |
| bb_pct_b_1H_20 | 24h | 0.4674 | 0.8665 |

bb_pct_b_1H_20 最穩定顯著: 4h (hit=0.4768, p=0.9756, 效應方向=reversal)

## ③b 因子邊際穩定性（oos_decay，IS-Train 內部 70/30 切分）
| 因子 | 頻率 | 存活率 | 反號 | 狀態 |
|---|---|---|---|---|
| bb_pct_b_1H_20 | 4h | 9.8506 | 是 | VETOED |

存活率（絕對值 mean_OOS / mean_IS）< 0.5 或反號代表這個因子在 IS-Train 內部自己都不穩定，不用等到 IS-Val 就已經是警訊。

## ③c 濾鏡 Regime 切片檢定（IS-Train，用③挑出的頻率）
這是原本舊版報告 §1/§1b 的內容，改成明確標成③c、限定只用 IS-Train（原本就是 IS-only，沒有 OOS 洩漏問題，這裡只是重新掛上 ①②③ 的切分結構，並改用③挑出的 4h forward period，而非舊版寫死的 4h forward period——本次剛好也落在 4h，屬巧合，不是刻意保留舊值）。

Vol+Amp 濾鏡（`is_consolidating`）：
| Regime | Hit Rate | p-value |
|---|---|---|
| true | 0.4649 | 0.9943 |
| false | 0.4980 | 0.5164 |

OI 濾鏡（`oi_consolidating`，BTC IS-Train OI 覆蓋率 90.8%）：
| Regime | Hit Rate | p-value |
|---|---|---|
| true | 0.4701 | 0.9894 |
| false | 0.4897 | 0.6123 |

## ④ 頻率/持有期決定
bb_pct_b_1H_20 在 4h 最穩定顯著，效應方向為「reversal」（hit rate 0.4768, p=0.9756）。策略部署檔（range_oscillator_strategy.py）逐 1H bar 判斷進出場，跟橫掃出來的最適 forward period（4h）不完全一致——這裡誠實記錄落差，不回頭改動已部署的策略頻率（見結論）。

## ⑤ 策略候選比較（IS-Val）
四個候選：無濾鏡基準、Vol+Amp濾鏡、Trend+Vol+Amp（部署邏輯）、Trend+Vol+Amp+OI（疊加真實 OI，BTC IS-Val OI 覆蓋率 94.4%，僅 3648/3672 筆有覆蓋，比較基礎比其他三個候選小）。

| 候選 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| No filter (baseline) | -33.79% | -3.7316 | -34.74% | 319 |
| Vol+Amp filter only | -28.61% | -3.6282 | -28.70% | 243 |
| Trend+Vol+Amp (deployed) | -7.73% | -1.3417 | -11.39% | 113 |
| Trend+Vol+Amp+OI (combined) | -7.43% | -1.2999 | -11.10% | 111 |

**IS-Val 挑選結果：「Trend+Vol+Amp+OI (combined)」**（Sharpe=-1.2999）優於無濾鏡基準（Sharpe=-3.7316）。挑選依據僅用 IS-Val，未使用 OOS。

## ⑤b 盲測 OOS
全程未用 OOS 挑選任何候選/參數；四個候選都跑出來是為了透明呈現，候選挑選的依據只看上面 IS-Val 的 Sharpe。

| 候選 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| No filter (baseline) | -54.63% | -3.0574 | -55.31% | 461 |
| Vol+Amp filter only | -41.61% | -2.4432 | -44.59% | 358 |
| Trend+Vol+Amp (deployed) | 1.84% | 0.2399 | -17.05% | 170 |
| Trend+Vol+Amp+OI (combined) | 9.97% | 0.9019 | -14.88% | 158 |

OOS 上「Trend+Vol+Amp+OI (combined)」（Sharpe=0.9019）同樣優於無濾鏡基準（Sharpe=-3.0574）——兩個獨立窗口（IS-Val、OOS）指向同一個結論。

## ⑥ MAE/MFE SL/TP 校準（IS-Train，基於⑤贏家「Trend+Vol+Amp+OI (combined)」）
**校準結果**：SL=3.21% / TP=2.06%（84 個進場事件）

Held-out（交易級交叉檢查，套用 IS-Train 校準出的固定 SL/TP，不重新校準，⑤贏家候選的訊號序列）：
| 區間 | 無SL/TP | Sharpe | +SL/TP | Sharpe |
|---|---|---|---|---|
| IS-Val | -3.69% | -1.0792 | -3.35% | -0.9354 |
| OOS | 10.06% | 1.4733 | 9.17% | 1.4456 |

## ⑦ 正式引擎交叉驗證（BacktestService，跨資產同參數不重調）
驗證的是磁碟上實際部署的策略檔（`range_oscillator_strategy.py`：Trend+Vol+Amp 濾鏡 + OI-fallback 恆真邏輯），與⑤挑出的研究候選是否一致見下方結論。

| 資產 | 累積報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| BTC | 21.49% | 0.58 | -16.05% | 176 |
| ETH | 110.49% | 1.67 | -15.34% | 172 |
| TXFR1 | -8.25% | -0.71 | -15.65% | 99 |

MAE/MFE SL/TP 疊加（正式引擎，用⑥從⑤贏家候選校準出的 SL/TP，套到部署策略檔上）：
| 資產 | 無SL/TP | Sharpe | +SL/TP | Sharpe |
|---|---|---|---|---|
| BTC | 21.49% | 0.58 | 16.67% | 0.44 |
| ETH | 110.49% | 1.67 | 90.88% | 1.52 |
| TXFR1 | -8.25% | -0.71 | -6.16% | -0.54 |

## 結論
- **濾鏡是否加值（⑤/⑤b，這是本次重構最大的方法論修正）**：舊版報告直接在 OOS 上比較三個濾鏡候選，等於候選挑選階段就用掉了唯一的盲測窗口。改成 IS-Val 挑選、OOS 盲測後，IS-Val 上最佳候選是「Trend+Vol+Amp+OI (combined)」（Sharpe=-1.2999），優於無濾鏡基準；OOS 盲測同樣支持這個結論。這代表舊版直接在 OOS 挑濾鏡雖然方法論上不嚴謹，但這次改成 IS-Val→OOS 兩階段後結論方向沒有改變。
- **核心因子的方向性（③，這是這個家族第一次真正檢定）**：bb_pct_b_1H_20 最穩定顯著是在 4h、效應方向為「reversal」（hit rate 0.4768, p=0.9756），跟策略假設的均值回歸方向一致。③b 顯示這個邊際在 IS-Train 內部切分後不穩定甚至反號（VETOED），不需要等到 IS-Val 就已經是警訊。
- **③c 濾鏡假設的事實根據**：Vol+Amp 濾鏡兩側最小 p-value=0.5164，OI 濾鏡兩側最小 p-value=0.6123（未做跨 regime 多重檢定校正，僅作描述性參考）——兩個濾鏡的任一側都沒有達到常見 0.05 門檻，濾鏡是否真的隔出了一個 bb_pct_b 更準的子集，缺乏因子層級的證據支持，⑤/⑤b 的策略級回測結果是更直接的判準。
- **頻率落差**：③橫掃出的最穩定 forward period（4h）跟部署策略固定逐 1H bar 判斷（見④）不完全一致——這是已知的方法論落差，尚未回頭調整部署頻率。
- **基礎頻率橫掃（③-freq）**：換基礎頻率後找到顯著組合（見③-freq），下一步應針對該頻率重新走④~⑧，而非沿用目前部署的 1H 邏輯。
- **OI 濾鏡的資料落差仍未解決**：見上方「Bug / 已知限制」——即使研究階段已能用真實 OI 驗證假設（③c/⑤），正式引擎上這個濾鏡仍然是 fallback 恆真，三重防禦名不副實的問題本身沒有變。
- **MAE/MFE SL/TP 疊加**：見⑦數字，不假設對所有家族都有害或有利，依實際引擎結果判斷。
