# Adaptive Switching — 回溯驗證報告

| 項目 | 內容 |
|---|---|
| 決策資產 | BTCUSDT |
| 跨資產穩健性 | ETHUSDT（同參數不重調） |
| 基礎頻率 | H1（gate=1D） |
| 樣本切分 | IS-Train 2024-01-01~2024-12-31 / IS-Val ~2025-08-31 / OOS ~2026-07-01 |

未建立 `strategy.py`（因子驗證未過）。

## 策略結構

三個候選，皆由 `prepare_signals(mode=...)` 產生：

- **Momentum-only**：日線 `mom_1D_10` 符號決定多空方向，`mom_1H_12` > 0.5% 進場、< -0.2%（多）出場，空單對稱。
- **RSI-only**：同一個日線方向 gate，RSI(14) < 30 進場多單、> 65 出場；RSI > 70 進場空單、< 35 出場。
- **Adaptive switching**：逐 bar 依波動 regime（ATR 比值分類的 high_vol/low_vol）選子策略——高波動用 momentum 訊號、低波動用 RSI 訊號。

Regime 分類器改用 `strategies.module.data.regime.compute_vol_regime`（原始研究的成交量比值定義只適用 24/7 連續市場，在非連續交易時段會崩潰）——同一個「用波動 regime 切換」的假設，換成已驗證、無前視偏誤的共用實作。

## 因子顯著性：mom_1h / rsi_demeaned 多頻率橫掃（IS-Train）

| 因子 | Forward(h) | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|
| mom_1h | 1 | 0.4908 | 0.9737 | 0.3685 |
| rsi_demeaned | 1 | 0.4860 | 0.9985 | **0.0235（PASS）** |
| mom_1h | 4 | 0.4899 | 0.8586 | 1.0000 |
| rsi_demeaned | 4 | 0.4882 | 0.9037 | 1.0000 |
| mom_1h | 12 | 0.5190 | 0.1530 | 1.0000 |
| rsi_demeaned | 12 | 0.5025 | 0.4945 | 1.0000 |
| mom_1h | 24 | 0.5127 | 0.3446 | 1.0000 |
| rsi_demeaned | 24 | 0.5127 | 0.4107 | 1.0000 |

Holm 校正（n=8）後唯一通過的格子：`rsi_demeaned @ h=1`，hit rate 0.4860 < 0.5——RSI 去均值後的符號越極端，下一小時報酬方向反而相反，符合均值回歸假設。`mom_1h` 在任何 horizon 上都未通過校正。

## 因子邊際穩定性（oos_decay，IS-Train 內部 70/30 切分）

| 因子 | Horizon | 存活率 | 反號 | 狀態 |
|---|---|---|---|---|
| mom_1h | 1h | 4.0736 | 是 | VETOED |
| rsi_demeaned | 1h | 0.3159 | 否 | VETOED |

兩個因子都被否決：`mom_1h` 前後半段效應方向相反；`rsi_demeaned` 存活率 0.3159 遠低於 0.5 門檻——**唯一通過 Holm 校正的格子在 IS-Train 內部自己都不穩定**，不構成可用邊際。

## Vol-regime 切片檢定（IS-Train，h=1）

| 因子 | Regime | Hit Rate | p-value |
|---|---|---|---|
| mom_1h | low_vol | 0.4852 | 0.9915 |
| mom_1h | high_vol | 0.4969 | 0.6515 |
| rsi_demeaned | low_vol | 0.4852 | 0.9955 |
| rsi_demeaned | high_vol | 0.4869 | 0.9574 |

兩個因子在 high_vol/low_vol 兩側 p 值都遠大於 0.05，**沒有任何切片顯示顯著方向性**——直接削弱切換機制的核心假設：如果動量該在高波動有效、RSI 該在低波動有效，這裡應該至少有一側顯著。

## 策略候選比較（IS-Val，BTCUSDT，零成本）

| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| Momentum-only | +4.25% | 0.3552 | -14.42% | 212 |
| RSI-only | +21.99% | 1.0938 | -18.43% | 32 |
| Adaptive switching | -0.73% | 0.1261 | -20.25% | 152 |

RSI-only 明顯最好，Adaptive switching 三者中最差且淨報酬為負。

## OOS 盲測（BTCUSDT，零成本）

| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| Momentum-only | +19.58% | 0.8392 | -17.64% | 266 |
| RSI-only | -4.19% | -0.0338 | -23.88% | 40 |
| Adaptive switching | +1.13% | 0.2016 | -25.32% | 196 |

**排序反轉**：IS-Val 最好的 RSI-only 在 OOS 變最差（典型過擬合），IS-Val 最差的 Momentum-only 在 OOS 反而最好。Adaptive switching 在兩個窗口都沒有優於當期最好的單一子策略。

## MAE/MFE 分布（switch 模式，IS-Train 進場事件）

268 個進場事件：median MAE = -1.00% / median MFE = 1.30% / P75 |MAE| = 1.82%。因子本身不穩定，僅記錄分布，不套用 SL/TP。

## 跨資產穩健性（ETHUSDT，同參數，不重調，零成本）

IS-Val：

| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| Momentum-only | +156.50% | 2.8874 | -22.95% | 238 |
| RSI-only | +22.56% | 0.9004 | -36.71% | 30 |
| Adaptive switching | +130.22% | 2.7089 | -21.45% | 149 |

OOS：

| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| Momentum-only | -15.18% | -0.2212 | -34.57% | 316 |
| RSI-only | -10.77% | -0.1140 | -37.29% | 48 |
| Adaptive switching | -20.32% | -0.4083 | -42.24% | 215 |

ETH 上 IS-Val 三個版本都是正報酬，但 **OOS 全部翻負**，且 Adaptive switching 是三者中最差——跟 BTC 模式不同，沒有任何候選同時在 BTC+ETH、IS-Val+OOS 四種組合下都站得住腳。

## 結論

- **切換機制沒有加值**：BTC 上兩個獨立窗口都沒有優於當期最好的單一子策略；ETH 上 IS-Val 表現不錯，但 OOS 是三者中最差。
- **唯一通過 Holm 校正的因子（`rsi_demeaned @ h=1`）沒有通過內部穩定性檢查**（存活率 0.3159），不構成可推廣邊際。
- **Regime 切片檢定沒有支持切換假設**：高低波動兩側 p 值都遠高於 0.05——切換的前提本身缺乏資料支持,不只是門檻沒調好。
- **RSI-only 在 BTC/ETH 的 IS-Val 都是典型過擬合訊號**：IS-Val Sharpe 不錯（BTC 1.09 / ETH 0.90），OOS 都轉負。Momentum-only 則資產間不一致。
- 以上皆為零成本數字，交易次數（32~316 筆）疊加真實成本後，本來就薄弱或不一致的邊際大概率進一步惡化。

**建議：不建議上線 Adaptive switching，也不建議把 RSI-only/Momentum-only 任一單獨拿去部署**——三個候選都沒有同時在 BTC+ETH、IS-Val+OOS 四種組合下一致站得住腳，因子層面也沒有找到穩健、方向正確、通過雙重檢查的邊際。

## 已知限制

- 因子顯著性用固定 h=1（橫掃出的最穩定值），但部署邏輯逐 bar 用連續門檻判斷進出場，不是預測固定 horizon 方向。
- 只驗證了 BTC 決策 + ETH 穩健性，未測非連續交易市場資產。
- MAE/MFE 分布只做診斷用途，因子本身不穩定，未疊加 SL/TP 回測驗證。
- 未做動量/RSI 門檻本身的橫掃（0.5%/-0.2%、30/65/70/35 是否最優）——本報告驗證的是原始部署參數。
