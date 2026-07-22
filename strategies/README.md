# strategies/experiments/ 說明

一個資料夾 = 一個探索過的想法。位置維持在 `strategies/experiments/`（本檔案跟
`RESEARCH_METHODOLOGY.md`、`FACTOR_ANALYSIS.md` 一起移到 `strategies/` 頂層方便追蹤，但
`experiments/` 資料夾本身沒有搬）。

跨 `experiments/` 的「這個因子測過沒有、目前是哪個分類」索引見 [`FACTOR_ANALYSIS.md`](FACTOR_ANALYSIS.md)
——**具體是哪個家族屬於下面哪一類、目前的驗證結論，一律以那份索引/各自的 `report.md` 為準，本檔只講分類規則本身**：

- **可在本 repo 直接執行、走 `RunConfig`/`librae` 的實驗**：有 `run.py`（可能還沒接 `config.yaml`）。
- **已用本 repo 真實資料/引擎驗證過、但沒通過的家族**：有 `utils.py` + `factor_research.py` 可直接執行，
  沒有 `strategy.py`（驗證通過才會搬去 `strategies/<name>/`，目前有沒有任何家族在那裡，看 `FACTOR_ANALYSIS.md`）。
  目前 `experiments/` 底下每個家族都屬於這一類——沒有任何家族只剩不可執行的歷史記錄。
- `kdj_oversold` 同時有 `run.py`（DB-first 訊號品質監控，持續累積）跟 `factor_research.py`（固定歷史窗口的
  一次性驗證）——兩者是互補的，不是重複：`run.py` 不因 `factor_research.py` 的結論而停止。

## 寫新的 `factor_research.py` 時注意

多重檢定校正直接用 `factrix.stats.holm_adjusted_p`（FWER）或 `romano_wolf_adjusted_p`（FWER，容許檢定間相依）——不要手刻。舊報告裡手刻 `holm_bonferroni` 的敘述是寫下當時 factrix 還沒公開這個 API 時的真實限制，保留不動，但不代表現在還要照做（見 `RESEARCH_METHODOLOGY.md` 因子分析章節）。

## `report.md` 撰寫規範

`report.md` 只呈現量化結果跟結論洞見，不寫過程敘事——不要交代「這份報告依照幾步驟流程執行」「這裡用了 X 工具而不是 Y 工具」「這是第一份驗證報告」之類的背景说明，也不留 bug 修復/工程細節（那些屬於 commit message，不屬於 report.md）。開頭用一個精簡的 Markdown 表格列決策資產、跨資產、頻率、樣本切分等中繼資料，不要用條列的粗體標籤。全文不使用 ①②③ 這類圓圈數字符號，段落標題直接用敘述性文字（如「因子顯著性」），不加編號前綴。可參考 `strategies/experiments/trendpullback/report.md` 的格式。

## 外部資料因子

資金費率、FNG/DXY regime、跨資產聯動、未平倉量走 `strategies/module/data/*`（`get_ohlcv()` 同一套
`timestamp`/`open`/`high`/`low`/`close`/`volume` 欄位慣例）：

- `module/data/funding.py` — `fetch_funding_rate_history()` / `attach_funding_features()`（Binance 永續資金費率，ccxt 公開端點免驗證）
- `module/data/open_interest.py` — `fetch_open_interest_history()` / `attach_oi_features()`（`data.binance.vision` 每日歸檔，DB-backed cache，走 `TIMESCALE_DSN`——沒有月度打包，每天一個檔案，第一次抓多年窗口前務必確認 `.env` 有被實際 source 進 shell，否則會靜默退回無快取的逐日重抓）
- `module/data/cross_asset.py` — `attach_cross_asset_features()`（跨資產滾動相關性/相對動能，呼叫 `get_ohlcv()` 抓參考資產）
- `module/data/regime.py` — `compute_vol_regime()`（`pandas_ta_classic.atr`）/ `fetch_historical_fng()` / `fetch_dxy_trend()`（需要 `pip install -e '.[research]'` 裝 `yfinance`）/ `attach_regime_columns()`
