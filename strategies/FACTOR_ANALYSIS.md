# Factor Analysis Index

一句話索引：在這裡查「這個因子/這個資產/這個頻率是不是已經測過」，細節永遠去對應的
`report.md` 看，本檔不重複存數字、不做結構化 schema——純文字表格，寫報告時手動加一行即可。

跟 `strategies/experiments/RESEARCH_METHODOLOGY.md`（①~⑧流程定義）、
`strategies/experiments/README.md`（`experiments/` 資料夾本身的說明）是三份不同定位的文件：
本檔橫跨 `experiments/`（已下定論的歷史記錄）跟已部署策略（例如 `trendpullback/`）的因子驗證
報告，是唯一涵蓋全部的索引。

| 家族 / 位置 | 測過的因子 | 資產 / 頻率 | 一句話結論 |
|---|---|---|---|
| [`trendpullback`](trendpullback/report.md) | `entry_signal`（EMA pullback + HTF trend gate，`gate_timeframe` 可調） | BTCUSDT H1+D1（決策）/ ETHUSDT（穩健性）/ BTCUSDT M5+M30（原 `trendpullback_m5`，已合併） | **H1、M5 都不顯著**（M5 唯一顯著格是方向相反）；HTF gate 兩個頻率 OOS 都比無濾鏡差，M5 OOS 又比 H1 更差——不建議維持現狀，兩個頻率都不建議 |
| [`experiments/mtf_trend_rsi`](experiments/mtf_trend_rsi/report.md) | `mom_1D_10`（日線動量）、`rsi_demeaned` | BTC/ETH/TXFR1，12h 持有期 | `mom_1D_10` **顯著**（Holm 校正後 p=0.0007）；`rsi_demeaned` 不顯著 |
| [`experiments/mtf_trend_momentum`](experiments/mtf_trend_momentum/report.md) | MTF Trend + Hourly Momentum vs 無濾鏡基準 | BTC/ETH | 有濾鏡版本在 IS-Val、OOS 都優於無濾鏡基準 |
| [`experiments/range_oscillator`](experiments/range_oscillator/report.md) | Trend+Vol+Amp+OI 組合濾鏡 vs 無濾鏡基準 | BTC | 組合濾鏡在 IS-Val、OOS 都優於無濾鏡基準 |
| [`experiments/funding_crowding_reversal`](experiments/funding_crowding_reversal/report.md) | Funding rate crowding、跨資產相關性/動能 | BTC（決策）/ ETH | 外部資料（funding/跨資產）沒有比純 OHLCV 基準更好，不採用 |
| [`experiments/adaptive_switching`](experiments/adaptive_switching/report.md) | RSI-only / Momentum-only / 兩者間 regime 切換 | BTC | 切換機制沒有加值，IS-Val、OOS 都劣於較好的單一子策略 |
| [`experiments/mtf_trend_slicing_regime`](experiments/mtf_trend_slicing_regime/report.md) | Regime filter vs 無濾鏡基準 | BTC | IS-Val、OOS 排序不一致（互相矛盾），濾鏡加值與否不穩定，不可靠 |
| [`experiments/mtf_4h_regime_reversal_funding`](experiments/mtf_4h_regime_reversal_funding/report.md) | `vwap_dist_12`、funding_z regime 切換複合策略 | BTC，4H/16h 持有期 | 報告自行宣稱 regime-switching 複合策略最佳（見原報告細節，格式與其他報告差異較大，未獨立覆核） |

## 使用方式

- **開新因子研究前**：先掃一眼上表有沒有類似的因子/資產/頻率組合，避免重測同一件事；細節/實際數字一律去 `report.md`，不要只看這裡的一句話就下結論。
- **寫完新報告後**：在上表加一行。不用額外欄位、不用 commit pin、不用 JSON metadata——這張表只負責「指路」，數字的正確性/多重檢定校正全部留在原報告裡。
- **共用研究工具**：泛用的（IS/Val/OOS 切分）在 `strategies/module/utils.py`；因子檢定專用的
  （factrix event panel 組裝、Holm 校正列印）在 `strategies/module/factor.py`。策略專屬的訊號
  計算（EMA/RSI/momentum 等）留在各策略自己的 `utils.py`，不要混進共用模組。
