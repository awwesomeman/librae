# Funding-Rate Crowding Reversal — 研究報告

**時間**: 2026-07-12 | **決策資產**: BTC | **跨資產**: BTC, ETH
**樣本切分**: IS-Train 2024-08-01~2025-04-30 | IS-Val ~2025-09-30 | OOS ~2026-06-01 | 跨資產窗口 2025-01-01~2026-06-01（同參數不重調）

流程依 `strategies/single_asset/README.md` 的 ①~⑧ 順序執行。

## ① 資產/資料層

本研究要納入兩類外部數據：**籌碼/部位資訊**與**跨資產連動性**。逐一盤點本專案現有的三個資產（`utils/universe.py`）：

| 資產 | 籌碼類數據可得性 | 跨資產連動可得性 |
|------|-----------------|-----------------|
| BTC/ETH（Crypto，binance） | **永續合約資金費率（funding rate）**：`ccxt.binanceusdm`，公開、免驗證、歷史完整回溯到合約上市 — 這是最接近「持倉/籌碼」語意的公開數據（資金費率持續為正 = 槓桿多頭擁擠，需要付費維持部位） | ETH 本身就在 `TICKERS` 裡，同交易所同頻率，直接可配對 |
| TXFR1（TAIFEX 近月連續） | 台指期真正的籌碼數據（三大法人買賣超、未平倉）在本專案**完全沒有現成的資料來源封裝**，且連基礎 OHLCV 都已經綁定一組必須先連線的 Shioaji 憑證（`utils/universe.py` 的既有註解） | 需要额外接一個非 crypto 的參考資產（如加權指數、美元兌台幣），同樣沒有現成封裝 |

另外測試了 Binance 的未平倉量歷史（open interest history）API，結果：**只保留最近約 30 天**（`startTime` 超出這個窗口會被交易所直接回 400），沒辦法覆蓋本框架慣用的多月 IS-Train 窗口，所以放棄，只用資金費率作為籌碼代理。

**結論：crypto（BTC 為主，ETH 交叉驗證）取得成本明顯低於 TXFR1，本輪先做 BTC。** TXFR1 的真實籌碼數據留給下一輪迭代（需要先建 TAIFEX/TWSE 開放數據的封裝）。

新增外部因子：`funding_rate_bps`（原始資金費率換算 bps）、`funding_z_3d`（資金費率相對近 3 天自身分布的 z-score，衡量「擁擠程度」）、`funding_cum_3_bps`（近 3 次結算的累積資金費率，衡量「擁擠是否持續」）、`xasset_corr_24`（與 ETH 24 小時滾動報酬相關性）、`xasset_relmom_24`（相對 ETH 的 24 小時超額動能）。

## ② 樣本切分

IS-Train (2024-08-01 ~ 2025-04-30) 做因子篩選；IS-Val (2025-04-30 ~ 2025-09-30) 做候選策略挑選；OOS (2025-09-30 ~ 2026-06-01) 盲測，全程未用 OOS 回頭調整任何因子/候選/參數。

## ③ 外部數據因子篩選（IS-Train，`factrix.evaluate_horizons` + `directional_hit_rate`，1H）

| 因子 | 持有期 | Hit Rate | p-value | 效應方向 |
|------|-------|----------|---------|---------|
| （無顯著因子） | - | - | - | - |

## ③b 因子邊際穩定性（oos_decay，IS-Train 內部 70/30 切分）

| 因子 | 頻率 | 存活率 | 反號 | 狀態 |
|---|---|---|---|---|
| funding_rate_bps | 12h | 0.0656 | 否 | VETOED |
| funding_z_3d | 24h | 12.4081 | 否 | PASS |
| funding_cum_3_bps | 1h | 0.9773 | 是 | VETOED |
| xasset_corr_24 | - | - | - | not_applicable (degenerate at every horizon in ③) |
| xasset_relmom_24 | 1h | 0.0484 | 是 | VETOED |

存活率（絕對值 mean_OOS / mean_IS）< 0.5 或反號代表這個因子在 IS-Train 內部自己都不穩定，不用等到 IS-Val 就已經是警訊。

## ③-freq 基礎頻率橫掃（IS-Train，1H/4H/1D，僅 `xasset_corr`/`xasset_relmom`）

`xasset_corr_24`/`xasset_relmom_24` 是純 OHLCV 衍生因子（滾動報酬相關性/相對動能），可以合法在其他基礎頻率重新計算，直接呼叫 `get_crypto_kbars_df` 取 BTC/ETH 在 4H/1D 的 K 線重新計算，而非透過綁定 1H 的 `attach_cross_asset_features`/`load_ohlcv`。

**`funding_rate_bps`/`funding_z_3d`/`funding_cum_3_bps` 未包含在這個橫掃裡**：資金費率結算在 Binance 固定的每日 3 次（00:00/08:00/16:00 UTC）排程上，跟 OHLCV 基礎頻率無關 —— 沒有「4H 資金費率」或「1D 資金費率」這種東西可以重新計算，只能把同一個 3-次/日印花序列重新取樣貼到不同粗細的價格 K 線上，這不構成真正的頻率假設檢定。本節誠實排除這兩個因子的橫掃，而非假裝測過。

**多重檢定校正方法**：用 `utils/stats.py` 的手刻 `holm_bonferroni`（Holm step-down，控制 **FWER**），不是 factrix 現成工具。原因：這個網格搜尋的決策形態是「掃過 base_tf×factor×horizon 全部組合、挑單一贏家」，需要 FWER（控制「至少挑錯一次」的機率），不是 FDR（控制「誤判佔比的期望值」，適合「篩一批因子、全部留著用」的情境）。**factrix 公開 API 目前只有 FDR 工具**（`fx.stats.bhy_adjusted_p`/`fx.multi_factor.bhy`/`bhy_hierarchical`）——`factrix/_stats/multiple_testing.py` 雖然有 `holm_step_down`/`bonferroni`，但那是底線開頭的 private module，未被 `factrix/__init__.py` 或公開的 `factrix/stats/` 引用，不能當作穩定 API 依賴。這是 README ③ 認定的真實 factrix 缺口。

| 基礎頻率 | 因子 | Forward(bars) | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|---|
| 1h | xasset_relmom | 1 | 0.5074 | 0.1705 | 1.0000 |
| 1h | xasset_relmom | 4 | 0.5017 | 0.6072 | 1.0000 |
| 1h | xasset_relmom | 12 | 0.4830 | 0.8059 | 1.0000 |
| 1h | xasset_relmom | 24 | 0.4960 | 0.5820 | 1.0000 |
| 4h | xasset_corr | 1 | 0.5212 | 0.2351 | 1.0000 |
| 4h | xasset_relmom | 1 | 0.5058 | 0.4175 | 1.0000 |
| 4h | xasset_corr | 2 | 0.5055 | 0.7780 | 1.0000 |
| 4h | xasset_relmom | 2 | 0.5179 | 0.1743 | 1.0000 |
| 4h | xasset_corr | 3 | 0.5379 | 0.0210 | 0.8402 |
| 4h | xasset_relmom | 3 | 0.5213 | 0.2132 | 1.0000 |
| 4h | xasset_corr | 6 | 0.5444 | 0.5500 | 1.0000 |
| 4h | xasset_relmom | 6 | 0.5444 | 0.1099 | 1.0000 |
| 1d | xasset_corr | 1 | 0.4796 | 0.8995 | 1.0000 |
| 1d | xasset_relmom | 1 | 0.4981 | 0.5739 | 1.0000 |
| 1d | xasset_corr | 2 | 0.4851 | 0.8741 | 1.0000 |
| 1d | xasset_relmom | 2 | 0.5672 | 0.0798 | 1.0000 |
| 1d | xasset_corr | 3 | 0.4607 | 0.9608 | 1.0000 |
| 1d | xasset_relmom | 3 | 0.4382 | 0.9194 | 1.0000 |
| 1d | xasset_corr | 5 | 0.6038 | 0.1994 | 1.0000 |
| 1d | xasset_relmom | 5 | 0.6415 | 0.0360 | 1.0000 |

Holm-Bonferroni 校正（n=20 個檢定，跨 base_tf × factor × horizon 一起校正，alpha=0.05）後，沒有任何組合顯著。1H/4H/1D 三個基礎頻率上，xasset_corr/xasset_relmom 皆未通過校正後的顯著性門檻——換粗/細基礎頻率沒有找到可用的邊際。

## ④ 頻率/持有期決定

換 4H/1D 重新取資料橫掃 xasset_corr/xasset_relmom 後（跨 base_tf×factor×horizon 做 Holm-Bonferroni 校正），三個基礎頻率（1H/4H/1D）上都沒有通過校正後的顯著性門檻——這不是 1H 起始假設選錯，換基礎頻率沒有找到可用邊際。 funding 系列因子（funding_rate_bps/funding_z_3d/funding_cum_3_bps）結構上無法做這個橫掃：資金費率結算在固定的每日 3 次（00:00/08:00/16:00 UTC）排程上，跟 OHLCV 基礎頻率無關，沒有「4H/1D 版本的資金費率」可以重新計算，只能把同一個 3-次/日的印花序列重新取樣貼到不同粗細的價格 K 線上，不構成真正的頻率假設檢定，因此本節誠實排除、不假裝橫掃過。維持 1H 作為部署頻率——這跟本策略家族的其他候選（OHLCV-only baseline）沿用的頻率一致，既有的正式引擎部署檔（ohlcv_baseline_strategy.py）本就是逐 1H bar 判斷進出場。

## ⑤ 策略候選比較（IS-Val，BTC）

| 候選 | 累積報酬 | Sharpe | 最大回撤 |
|------|---------|--------|---------|
| A: Funding Crowding Reversal (pure contrarian) | 8.47% | 0.8349 | -17.99% |
| B: Funding Reversal + Cross-Asset RelMom Confirm | 6.13% | 0.7934 | -13.23% |
| C: OHLCV-only baseline (no external data) | 18.58% | 1.8013 | -8.16% |

**勝出**: C: OHLCV-only baseline (no external data)（Sharpe=1.8013）

## ⑤b 盲測 OOS（BTC，2025-09-30 之後）

| 指標 | 數值 |
|------|------|
| 策略累積報酬 | 51.83% |
| 大盤同期報酬 | -35.47% |
| Sharpe | 1.9590 |
| 最大回撤 | -16.49% |

## ⑥ MAE/MFE 停損停利疊加（IS-Train 校準，IS-Val/OOS 驗證）

**校準結果**：SL=3.20% / TP=2.08%（234 個進場事件）

| 區間 | 無SL/TP 報酬 | 無SL/TP Sharpe | +SL/TP 報酬 | +SL/TP Sharpe |
|------|--------------|-----------------|--------------|-----------------|
| IS-Val | 15.39% | 1.7041 | 25.40% | 2.7560 |
| OOS | 45.72% | 1.6803 | 40.50% | 2.0144 |

## ⑦ 正式引擎交叉驗證（`BacktestService.run()`）— 僅限「無外部數據」基準

| 資產 | 累積報酬 | Sharpe | 最大回撤 | 交易次數 |
|------|---------|--------|---------|---------|
| BTC | 10.06% | 0.23 | -25.74% | 450 |
| ETH | 343.11% | 1.84 | -28.08% | 537 |

**為什麼 A/B（資金費率型候選）沒有正式引擎數字**：`BacktestService._execute_indicator()` 透過 `backend_api_python/app/utils/safe_exec.py` 的沙箱 `exec()` 執行 `# @strategy` 腳本，其 `SAFE_IMPORT_MODULES` 白名單只有 `{numpy, pandas, math, json, datetime, time, collections, functools, itertools, statistics, decimal, fractions, copy}` — **沒有 `requests`/`ccxt`，完全沒有網路存取**。也就是說，資金費率或另一個資產的即時價格，在目前的正式引擎架構下，`# @strategy` 腳本**在訊號計算當下無法自行抓取** — 這不是這次研究省略的步驟，而是這條研究路線在「能不能真的上線交易」這一層目前踩到的實際限制。

## ⑧ 跨資產穩健性（BTC 決策，同參數套用 ETH，不重新調參）

⑦已涵蓋基準 C 的跨資產引擎數字；以下是外部因子（`mom_1H_12`）本身在 BTC/ETH 上是否都有方向性邊際的 factrix 檢定，因為 A/B 兩個候選本身無法透過⑦驗證。只有 2 個資產，正式跨切片假設檢定（`slice_pairwise_test`/`slice_joint_test`）統計上不夠力，用描述性排行榜 + Holm 校正取代：

| 資產 | Beta | p-value | Holm 校正後 p-value |
|------|------|---------|---------------------|
| BTC | -0.0007 | 0.8419 | 0.8419 |
| ETH | 0.0038 | 0.2476 | 0.4952 |

**手刻校正注記**（README ③）：`holm_bonferroni`（`utils/stats.py`）是手刻，不是 factrix 現成工具，因為（1）「檢查因子依賴性是否跨資產成立」需要 FWER（控制至少判斷錯一次的機率），不是 FDR；（2）factrix 公開 API 目前沒有 FWER 工具——`factrix/_stats/multiple_testing.py` 雖有 `holm_step_down`，但那是底線開頭的 private module，未被公開介面引用，不是穩定 API。

## ⑧c 跨資產持有期橫掃（`factrix.evaluate_horizons`，pooled cross-sectional IC）

| 持有期 | Pooled IC | p-value |
|-------|-----------|---------|
| 1h | -0.0201 | 0.0283 |
| 4h | -0.0325 | 0.0062 |
| 12h | -0.0561 | 0.4269 |
| 24h | -0.0611 | 0.0192 |

---

## 結論（誠實版）

1. **外部因子在 IS-Train 上沒有通過顯著性門檻**：五個新因子（`funding_rate_bps`/`funding_z_3d`/`funding_cum_3_bps`/`xasset_corr_24`/`xasset_relmom_24`）在 1/4/12/24 小時任何一個持有期上，p-value 都沒有跨過 0.05/0.95 的顯著性門檻 — 資金費率擁擠與跨資產相對動能，在目前樣本窗口上**沒有獨立於既有 OHLCV 因子集之外的方向性邊際**。這是有效的研究結論，不代表方法或數據管線有問題。
2. **③b 的 oos_decay 快篩**：見上表——任何存活率過低或反號的因子，代表這個邊際在 IS-Train 內部自己都不穩定，是比 IS-Val 更早的警訊。
3. **③-freq 換基礎頻率沒有拯救 xasset 因子**：1H 上的不顯著不是頻率選錯——換 4H/1D 重新取資料橫掃、跨 base_tf×factor×horizon 做 Holm-Bonferroni 校正後，三個基礎頻率上 xasset_corr/xasset_relmom 都沒有通過顯著性門檻，代表這兩個因子在 BTC 上（至少在這個樣本窗口）本身就不具備可用邊際，不是 1H 這個起始假設的問題。 funding 系列因子則因為結算排程跟 OHLCV 基礎頻率無關，結構上無法做這個橫掃，這是誠實的範圍限制，不是遺漏。
4. **候選 C（純 OHLCV，無外部數據）在 IS-Val/OOS 上直接贏過 A/B**：IS-Val Sharpe C=1.80 vs A=0.83 / B=0.79 — 跟前兩點一致：這次新增的兩類外部數據，無論是當獨立因子看（③）還是包成完整策略看（⑤），都沒有比既有 OHLCV-only 打法更好。「能不能納外部數據」這個問題，本輪的誠實答案是**能取得、能接進 factrix 流程，但目前沒有觀察到它加值**。
5. **可交易性缺口（⑦）是本次研究流程本身最重要的發現，獨立於上面統計結論**：即使某個未來版本的外部因子真的顯著，目前的正式回測/實盤引擎在架構上就無法讓策略腳本讀取資金費率或跨資產價格 — `BacktestService` 的沙箱 `exec()` 不允許任何網路存取。要讓這條研究路線真正可上線，需要工程面先讓 `BacktestService` 支援「額外數據欄位」注入（例如把 funding_rate 當成跟 OHLCV 一起餵給引擎的預先計算欄位），而不是讓策略腳本自己在沙箱裡發網路請求（那也不安全，不該開放）。
6. **下一步建議**：(a) 外部數據這條路線在 BTC/ETH 上目前沒有找到邊際，繼續在同樣的資金費率因子上調參屬於過度擬合同一批數據，不建議；更值得做的是換一種「籌碼」定義（例如現貨/合約成交量比、多空比 long/short ratio——Binance 也有免驗證的公開端點，這次沒測）。(b) TXFR1 的真實籌碼數據（三大法人、未平倉）值得作為下一輪外部數據研究的資產，但要先建立獨立於 Shioaji 即時憑證之外的歷史數據來源。(c) 不論哪個方向，⑦節的引擎沙箱限制都要先解決，否則統計上再顯著的外部因子也無法變成可上線的策略。
