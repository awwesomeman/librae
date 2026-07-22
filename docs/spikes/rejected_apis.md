# 不採用的資料 API 紀錄

> 收錄所有測過但決定不用的資料 API（不限資料類型），跟原因——已採用的因子直接看對應概念檔案（`strategies/module/data/*.py`）的 docstring，不在這裡重複。每次評估新增一段，不回溯刪除舊條目。

## 2026-07-22 — 美股另類資料源（社群情緒/國會交易/13F/分析師評等）

實測代表真的打過 API（不是只看文件），用 MU（Micron）當測試標的。

| 來源 | 想拿什麼 | 不採用原因 |
|---|---|---|
| House Stock Watcher | 國會議員交易 | `housestockwatcher.com` 網域 DNS 解析不出來，已死，二次確認 |
| senate-stock-watcher-data（GitHub） | 國會議員交易 | 最後一次更新 2021-03，已棄坑 5 年 |
| Financial Modeling Prep (FMP) | 國會交易/13F/insider trading | 文件與 demo key 實測皆確認這幾個端點在付費層，免費層只有 EOD 陽春資料 |
| Finnhub — congressional-trading / institutional ownership(13F) / social-sentiment / price-target / upgrade-downgrade / news-sentiment | 國會交易、機構持倉、社群情緒、目標價 | 用真實註冊的免費 key 實測，全部回 403（收費層限定） |
| Finnhub — insider-transactions | 內部人交易 | 免費層可用，但回應 `source:"sec"`——資料本身也是轉手 SEC 申報，跟 `us_insider.py` 直接讀 EDGAR Form 4 原始 XML 重複，我們的版本更正確（無第三方轉手延遲/解析誤差） |
| sec-api.io | 13F/Form 4 解析 | 付費導向，無免費層，未實測（判斷不需要） |
| OpenBB SDK | 多方整合 | 重框架，自帶一套資料模型/抽象，跟 repo「provider 只放純 API client、零商業邏輯」的架構慣例衝突，未實測（架構不合，不是資料可用性問題） |
| pytrends | Google Trends 搜尋量 | 實測第一次呼叫就被 Google 429；套件本身依賴的 urllib3 API 已被上游移除（`method_whitelist` 參數不存在了），庫早已無人維護 |
| python-jobspy | LinkedIn/Indeed 職缺數 | 實測技術上能跑（真的抓到 Micron 職缺），但 (1) 它宣告的依賴版本上限比目前 repo 的 numpy/pandas 舊，裝上去會強制把整個 repo 的依賴降版本；(2) 只能抓「現在」的快照，無歷史回填，要排程收集好幾個月才有信號價值 |
| SEC EDGAR 全文檢索（13F 機構持倉彙總） | 誰持有 MU、持股變化 | 實測 MU 單季 CUSIP 全文檢索就命中 3220 筆申報，而且全文檢索只回傳「有提到」的清單，沒有彙總持股數——要算總持股得逐筆抓每份 filing 的 `infoTable.xml` 解析加總，一季上千次 HTTP call，跟 Form 4（單一 issuer 一條 feed）完全不同量級，不是薄 client 能處理的規模。與 Finnhub/FMP 的 13F 端點一樣，判斷本來就該是收費服務的範疇 |
