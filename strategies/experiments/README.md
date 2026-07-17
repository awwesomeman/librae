# experiments/

一個資料夾 = 一個探索過的想法

- `kdj_oversold/`、`trendmaster/`：用本專案的 `RunConfig`/`librae` 跑的實驗，`run.py` 可直接執行。
- 其餘（`adaptive_switching/`、`funding_crowding_reversal/`、`mtf_*/`、`range_oscillator/`）：
  用 `factrix` 做因子研究後**已經有結論、大多是「不建議上線」**的策略家族，只保留研究腳本
  （`*_research.py`）跟 `report.md` 作歷史記錄。這些研究腳本引用的是另一個專案的
  `utils/`（`cached_kline.py`/`universe.py`/`backend_api_python` 等）——**在本 repo 裡不能直接執行**，
  純粹留著讓人看得到當初測了什麼、為什麼沒有採用，結論以 `report.md` 為準。每份 `report.md`
  都是照 `RESEARCH_METHODOLOGY.md`（①~⑧ 流程）做的，那份文件也一併保留在這裡。
- 唯一因子層級有通過統計檢定的部分（`mtf_trend_rsi` 的 `mom_1D_10` 日線趨勢濾網）若之後
  簡化成正式策略，會是 `strategies/` 底下全新的一個資料夾，不會直接改這裡的內容。

## 寫新的 `factor_research.py` 時注意

這幾份報告當初手刻 `holm_bonferroni` 是因為 factrix 當時（研究進行時）的 Holm/FWER 校正只在
`factrix/_stats/multiple_testing.py`（底線開頭，non-public）。**這個缺口已經解決**：
factrix `0.17.0` 起 `factrix.stats.holm_adjusted_p` 是正式公開 API（見 `pyproject.toml`，已把
`factrix` 依賴升到 `>=0.17`），還多了 Romano-Wolf（`romano_wolf_adjusted_p`，比 Holm 更寬鬆但一樣控制 FWER，適合檢定間有相依性的情況）。**之後新的因子研究不要再手刻，直接
`from factrix.stats import holm_adjusted_p`（或 `romano_wolf_adjusted_p`）。**舊報告裡「factrix
沒有 FWER 工具」的敘述在寫下當時是對的，保留不動——歷史記錄不回頭改。

## 外部資料因子——已經搬進 `data/`，可以直接用

這幾份研究用到的外部資料源（資金費率、FNG/DXY regime、跨資產聯動、未平倉量）已經照本專案的
`data.ohlcv.get_ohlcv()` 慣例（欄位 `timestamp`/`open`/`high`/`low`/`close`/`volume`，不是原本
yfinance 風格的 `date`/`Close`）重寫成可直接執行的模組，**不用再回頭抄 `strategies copy/utils/`**（已刪除）：

- `data/funding.py` — `fetch_funding_rate_history()` / `attach_funding_features()`（Binance 永續資金費率，ccxt 公開端點免驗證）
- `data/open_interest.py` — `fetch_open_interest_history()` / `attach_oi_features()`（`data.binance.vision` 每日歸檔，本地 parquet cache 於 `data/cache/`）
- `data/cross_asset.py` — `attach_cross_asset_features()`（跨資產滾動相關性/相對動能，改呼叫 `get_ohlcv()` 抓參考資產，不是原本的 `load_ohlcv`/`cached_kline.py`）
- `data/regime.py` — `compute_vol_regime()`（改用 `pandas_ta_classic.atr`，不重複造輪子）/ `fetch_historical_fng()` / `fetch_dxy_trend()`（需要 `pip install -e '.[research]'` 裝 `yfinance`）/ `attach_regime_columns()`

沒有搬的（有更好的既有替代，搬了只是重複）：`factors.py`（RSI/BB %B/ATR 用 `pandas_ta_classic` 即可）、
`universe.py`/`data.py`/`cached_kline.py`（用 `librae/config/symbols.yaml` + `data.ohlcv.get_ohlcv()`）、
`panel.py`/`engine_check.py`（綁定他們的 `BacktestService`，正式引擎交叉驗證要用 `librae.Backtest` 重新寫，見對話中討論的 `factor_research.py` 設計）。
