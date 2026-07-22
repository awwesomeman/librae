# Factor Analysis Index

一句話索引：在這裡查「這個因子/這個資產/這個頻率是不是已經測過」，細節永遠去對應的
`report.md` 看，本檔不重複存數字、不做結構化 schema——純文字表格，寫報告時手動加一行即可。

跟 `RESEARCH_METHODOLOGY.md`（流程定義）、`README.md`（`experiments/` 資料夾本身的說明）
是三份不同定位的文件：本檔是唯一涵蓋 `experiments/` 底下全部因子驗證報告的索引。**目前沒有任何
一個因子驗證通過的策略**——所有測過的家族都在 `experiments/` 底下，沒有已部署的 `strategy.py`。

| 家族 / 位置 | 測過的因子 | 資產 / 頻率 | 一句話結論 |
|---|---|---|---|
| [`experiments/trendpullback`](experiments/trendpullback/report.md) | `entry_signal`（EMA pullback + HTF trend gate，`gate_timeframe` 可調） | BTCUSDT H1+D1（決策）/ ETHUSDT（穩健性）/ BTCUSDT M5+M30 | H1、M5 都不顯著（M5 唯一顯著格是方向相反）；HTF gate 兩個頻率 OOS 都比無濾鏡差，不建議 |
| [`experiments/mtf_trend_rsi`](experiments/mtf_trend_rsi/report.md) | `mom_1D_10`（連續因子）、多空 `long_entry`/`short_entry`（RSI(14) 打門檻） | BTCUSDT（決策）/ ETHUSDT（穩健性），H1+D1 gate | `mom_1D_10` 修正前視偏誤後不顯著；進場訊號 Holm 校正後僅 3 格通過且全部方向相反，非可用邊際；不建立 `strategy.py` |
| [`experiments/mtf_trend_momentum`](experiments/mtf_trend_momentum/report.md) | `mom_1D_10` 日線濾鏡 + `mom_1H_12` 小時動量突破 | BTCUSDT（決策）/ ETHUSDT（穩健性） | 兩個連續因子皆不顯著；部署訊號在 IS-Train 有 4 格通過但 OOS 全數不顯著；OOS 回測看似轉正實為 `mom_1D_10` 方向性曝險，非 timing 邊際；不建立 `strategy.py` |
| [`experiments/range_oscillator`](experiments/range_oscillator/report.md) | `bb_pct_b`（Bollinger %b 均值回歸）+ Trend/Vol/Amp/OI 組合濾鏡 | BTCUSDT（決策）/ ETHUSDT（穩健性） | 因子唯一通過 Holm 校正的格子被 oos_decay 否決；IS-Val Sharpe 2.01 的組合濾鏡 OOS 轉負（-0.72），跨資產也不重現；不建立 `strategy.py` |
| [`experiments/funding_crowding_reversal`](experiments/funding_crowding_reversal/report.md) | 資金費率擁擠代理 + 跨資產相關性/相對動能 | BTCUSDT（決策）/ ETHUSDT（穩健性） | 五個外部因子在 IS-Train 全部不顯著；純 OHLCV 基準在 IS-Val 上直接贏過兩個外部數據候選；不建立 `strategy.py` |
| [`experiments/adaptive_switching`](experiments/adaptive_switching/report.md) | RSI-only / Momentum-only / 兩者間波動 regime 切換 | BTCUSDT（決策）/ ETHUSDT（穩健性） | 切換機制沒有加值，IS-Val、OOS 都劣於較好的單一子策略；regime 切片也不支持切換假設；不建立 `strategy.py` |
| [`experiments/mtf_trend_slicing_regime`](experiments/mtf_trend_slicing_regime/report.md) | `mom_1D_10` 閘門 + `rsi_1H_14` 觸發 + FNG/DXY/波動 regime 切片 | BTCUSDT（決策）/ ETHUSDT（穩健性） | 核心因子皆不顯著；regime 濾鏡加值與否 IS-Val/OOS 排序相反，不可靠；不建立 `strategy.py` |
| [`experiments/mtf_4h_regime_reversal_funding`](experiments/mtf_4h_regime_reversal_funding/report.md) | `mom_1D_10`、`vwap_dist_12`、`funding_z_3d` + regime 切換複合策略 | BTCUSDT（決策）/ ETHUSDT（穩健性），4H | 三個因子皆不顯著；原報告 headline 因子 `vwap_dist_12` 從未被實際計算過，重新實作後也不顯著；原報告「切換策略最佳」的宣稱未被獨立驗證支持（消融後更簡單的 trend_only 在 BTC 上更好）；不建立 `strategy.py` |
| [`experiments/kdj_oversold`](experiments/kdj_oversold/report.md) | KDJ(9,3) J<20 oversold（level-based 事件） | BTCUSDT（決策）/ ETHUSDT（穩健性） | 三段樣本方向不一致（IS-Train 正向、OOS 反向），濾網比無濾網基準差；`run.py` 的 DB 訊號監控持續進行但不視為已驗證；不建立 `strategy.py` |

## 使用方式

- **開新因子研究前**：先掃一眼上表有沒有類似的因子/資產/頻率組合，避免重測同一件事；細節/實際數字一律去 `report.md`，不要只看這裡的一句話就下結論。
- **寫完新報告後**：在上表加一行。不用額外欄位、不用 commit pin、不用 JSON metadata——這張表只負責「指路」，數字的正確性/多重檢定校正全部留在原報告裡。
- **共用研究工具**：泛用的（IS/Val/OOS 切分）在 `strategies/module/utils.py`；因子相關工具集中在
  `strategies/module/factors/`——公式目錄在 `library.py`（因子怎麼算，見其 docstring 的命名規則）、
  共用算子在 `operators.py`、因子檢定專用的（factrix event panel 組裝、Holm 校正列印）在
  `factors/utils.py`。策略專屬的訊號計算（EMA/RSI/momentum 等）留在各策略自己的 `utils.py`，不要混進
  共用模組。
