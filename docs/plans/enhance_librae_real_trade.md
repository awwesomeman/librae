# Plan: Real Trade Cost Enhancements

Status: **backlog** — 記錄真實交易相關的已知缺口與未來考慮項目；已完成的項目
會標記 ✅ 並保留在原本章節，方便追蹤演進，不另外搬移。

## Context

margin_rate 已完成（基本保證金對資本效率的影響），以下是更精細的真實交易成本，
在策略上線真實交易或需要更嚴格的回測模擬時再逐步加入。

---

## A. 借券費與利息 (Borrowing Fees / Rebates)

- 美股/台股做空需支付借券費 (Stock Loan Fee)，通常 annualized 0.3%~數十%
- 保證金留在經紀商處可能產生微薄利息 (Credit Interest)
- 融資買入時有利息支出
- **實作方向**: CostModel 加 `borrow_rate` / `financing_rate`，在 eval_equity 按持倉天數累計

## B. 維持保證金 (Maintenance Margin) — ✅ 已完成

- 持倉期間需監控: Equity = Margin + Unrealized_PnL
- 若 Equity < Maintenance_Margin → 觸發強制平倉 (Margin Call)
- 不考慮此點會過度樂觀地估計策略在劇烈波動下的存活能力
- **已實作**: `CostModel.maintenance_margin_rate` + `CostModel.liquidation_price()`
  (`librae/core/cost_model.py:229-250`)，`resolve_stop_exit()`
  (`librae/core/executor.py:345-378`) 每 bar 檢查、觸發時強制平倉——backtest
  與 live 共用同一路徑（`run_pending_and_stops`），不會有雙軌不一致的風險。
  2026-07-26 code review 確認生效。

- **設計選擇：用比率、不用絕對金額，且整個 run 內視為靜態**（2026-07-26 討論）。
  交易所真實機制是 `保證金 = f(波動度) × 名目價值`——名目價值本身每根 K 棒都
  在變。兩種靜態近似方式比較：
  - 凍結絕對金額：每次價格變動就跟著失準（交易所本來就沒把絕對金額當常數
    維護，TAIFEX 大台/小台/微台 636,000/159,000/31,800 TWD 換算成比率剛好
    對齊 20:5:1，證實交易所是拿比率當錨點，絕對金額只是比率 × 當時名目
    價值算出來的結果）。
  - 凍結比率：名目價值變動不影響它，只有交易所真的調整風險係數本身（波動度
    制度轉換）才會失準——發生頻率遠低於「每根 K 棒都在變」的名目價值。
  結論：比率是結構上更貼近實際的近似方式，這是採用比率、不做絕對金額欄位的
  理由，不只是「比較簡單」。
- **已知限制（尚未處理，不建議現在做）**：
  1. 比率近似仍會在「波動度制度轉換」時失準——而這種轉換往往發生在市場劇烈
     波動期，正好是回測最需要精準模擬保證金/強平風險的時候。強平價位對這種
     誤差的敏感度又不是線性的（`margin_rate - maintenance_margin_rate` 這個
     緩衝本來就薄，槓桿的意義就在這裡）。
  2. Crypto 永續合約的保證金實務上是依名目金額分級（tiered，部位越大維持
     保證金率越高），不是單一比率；現有 crypto `margin_rate=1.0` 等於完全
     沒有模擬槓桿保證金機制，是比「比率不隨時間更新」更大幅度的簡化。
  3. 目前沒有「絕對金額轉比率」的 helper——使用者拿到交易所公告的絕對金額
     （多數保證金公告本來就是用絕對金額發布），要自己手動換算成比率才能設
     進 `cost_overrides`/`symbol_overrides`（`market_config.py:92-104` 的
     `0.075` 就是團隊手動算過一次的紀錄）。真的需要精確模擬時（危機期壓力
     測試、crypto 高槓桿策略），用 `cost_overrides` 把回測拆段、每段套不同
     比率即可，不需要引擎新增機制。

## C. 股息處理 (Dividends)

- Long: 收到股息 (Cash Inflow)
- Short: 必須支付股息給出借方 (Cash Outflow) — 常被忽視的做空成本
- **實作方向**: 需要股息資料來源，engine 在除息日對持倉做 cash adjustment

---

## D. 偏誤控制與責任劃分 (Bias Control & Responsibility)

### D-1. 生存者偏誤 (Survivorship Bias) — 資料層

- Universe 必須使用 **Point-in-Time Data**：回測某日時，股票池須包含當時存在但日後下市的標的
- 資料庫須保留已下市 (Delisted) 股票的歷史行情
- **引擎的唯一任務**：在資料層標示「該股票已下市」時，以最後交易價（或歸零）強制清算部位

### D-2. 前瞻偏誤 (Look-ahead Bias) — 資料層 + 引擎

- **不應直接使用還原股價 (Adjusted Price) 做回測**：還原股價是以未來除權息資訊倒推，引入 look-ahead bias
- 資料層提供：**原始收盤價 (Unadjusted Price)** + **公司行動表 (Corporate Actions Table)**（除權息日期、股利金額）
- 引擎讀取原始價格觸發成交，並在除息日依持倉方向與數量計算現金流：
  - Long N 股 → 除息日 +N × 股利（現金流入）
  - Short N 股 → 除息日 −N × 股利（現金流出）
- 若在資料預處理階段將除權息「還原掉」，引擎無法準確模擬 cash flow 與保證金變化，導致槓桿倍數計算錯誤

### D-3. 責任矩陣

| 處理項目 | 負責階段 | 原因 |
|---|---|---|
| 生存者偏誤 | 資料準備 (Data Prep) | Universe 須隨時間動態變化，包含已下市標的 |
| Look-ahead Bias | 資料 + 策略邏輯 | 訊號計算只能使用 T 之前的資料 |
| 除權息補償 (Short Dividend) | 回測引擎 (Executor) | 與部位方向、大小相關，屬帳務結算 |
| 下市清算 (Liquidation) | 回測引擎 (Executor) | 非預期狀況的強制退場 |
| 資料缺失處理 (Data Gaps) | 資料清理 (Cleaning) | 引擎不應處理補值，由資料層確保一致性 |
| 連續合約換月調整 (Roll Adjustment) | 資料層 | `continuous_alias` symbol（`librae/config/symbols.py`）本身只代表「當下最近月」，不做價格銜接；換月日價格跳空需資料層自行處理，否則回測損益在換月日附近會失真 |

---

## E. 跨市場/跨貨幣 (Multi-Currency)

**現況（2026-07-26 code review 確認）**：`LiveTrader.__init__` 目前只自動接線
`tw_futures`（Shioaji）與其餘一律走 ccxt/crypto，沒有 IBKR 的自動接線；
`RunConfig.market` 是單一市場，一次 run 通常就是單一貨幣，這個問題目前架構上
幾乎不會被觸發。

**會踩到的情境**：只有真的寫一個跨市場套利策略（例如同時操作 TW 期貨 +
US 股票兩條腿）才需要在兩個貨幣間對齊損益。`brokers/ibkr_adapter.py` 目前寫死
`currency="USD"`，`CostModel`/`RunConfig` 完全沒有匯率轉換機制。

**建議**：不要在 core engine 預先蓋 FX 轉換基礎設施——目前沒有實際用例，屬於
過度設計。等真的要寫跨市場策略時，在該策略自己的 wrapper 層處理匯率轉換/損益
歸一即可。

## G. Limit 成交流動性假設 (Limit Fill Liquidity Assumption)

`resolve_fill_price()`（`librae/core/executor.py:499-531`）的數字型 `fill_price`
（限價單）假設只要限價落在該根 K 棒的 `[low, high]` 範圍內，就能以該價格
**全額**成交——不考慮實際成交量/委託簿深度。流動性好的市場（大型股、主流
加密貨幣）影響不大，但流動性差的市場會系統性高估限價單的成交機率與成交量。
已在該函式 docstring 註明；暫不實作委託簿深度模擬（過度設計，目前沒有明確
需要精細模擬的策略）。

## F. 券商對帳/一致性缺口 (Broker Reconciliation Gaps)

- **IBKR/Shioaji 現金對帳 — ✅ 已補上，但欄位語意未經真實帳號驗證**
  (2026-07-26)。`brokers/ibkr_adapter.py::get_balance`（`accountSummary()`
  查 `TotalCashValue`）、`brokers/shioaji_adapter.py::get_balance`
  （`margin()` 查 `equity_amount`/`available_margin`）都已實作，兩者
  docstring 都明確標註「UNVERIFIED against a live session」——本機沒有安裝
  `ib_async`、也沒有真實 TWS/Shioaji 帳號能確認欄位名稱與語意，只驗證過
  mock 測試的程式邏輯分支。**這只能在真的連上 Shioaji sandbox
  (`SHIOAJI_SANDBOX=true`) 或 IBKR paper trading（不需要正式環境/真錢）時
  才能驗證**——純 backtest 完全碰不到這條路徑（`_reconcile_cash` 只在
  `mode="live"` 且有 `order_adapter` 時才跑）。
  同時修掉一個連帶的結構性 bug：`_reconcile_cash` 原本只認得 CCXT
  `"BASE/QUOTE"` 格式的 symbol 來抓幣別，TW 期貨/美股 symbol（`TXFR1`/`MU`）
  沒有這個格式，就算加了 `get_balance()` 也永遠不會被呼叫——已改成
  `market → 結算幣別` 對照表（`LiveTrader._MARKET_CURRENCY`）。
- **Live 抓取的 session 範圍可能跟回測資料不一致**：`IBKRAdapter.fetch_ohlcv`
  新增了 `use_rth` 參數（預設 `False`，維持原行為）——但這個 adapter 沒辦法
  自己偵測 backtest 資料當初是用哪種 session 範圍產生的，呼叫者要自行保證
  live 抓取跟 backtest 資料來源用同一個 `use_rth` 設定。

## H. 資產設定 (Asset Config) 管理

- **維持靜態 registry，不做動態查詢** — 2026-07-26 討論過 IBKR 式「執行時動態
  向官方/交易所拿合約規格」的做法，最後決定不做，即使 crypto 有乾淨的公開
  API（Binance `exchangeInfo`，無需 API key）可以低風險地動態查。理由：
  既然 TAIFEX/CME 這類沒有乾淨公開 API 的來源終究得維護靜態 registry
  （官方網頁爬蟲脆弱、改版會默默壞掉，风险高於現在的寫死字典），乾脆全部
  統一走靜態 registry（`librae/config/symbols.py`），不要讓不同來源用不同
  機制。`RunConfig.symbol_overrides`/`cost_overrides` 的 override 逃生口
  維持不變。
- **tick_size 應該是價格的函數，不是每個資產的固定常數** — 台股 tick size
  隨價格級距變動（10 元以下 0.01、10-50 元 0.05、50-100 元 0.1...），這件
  事已經在 `librae/config/symbols.py:175-182` 的註解裡；正確做法應該是
  market 層級一張「價格區間 → tick_size」對照表，查詢時傳入當下價格，而不是
  每個 symbol 各自宣告一個死值。**未實作原因**：`CostModel` 目前是 run 開始
  時建一次的 frozen dataclass，`tick_size` 是固定欄位；改成價格區間查表，
  代表撮合/滑價計算等所有讀 `cost_model.tick_size` 的地方都要改成「傳入當下
  價格去查」——是真的要動 `CostModel` 結構的改動，不是小修。目前沒有實際
  台股策略卡到這個精度限制，先不做。
- **使用者無法檢視目前解析出來的 asset config — ✅ 已完成**（2026-07-26）。
  `librae.describe_symbols(cfg, symbols=[...])`（`librae/core/cost_model.py`）
  一次回報一批 symbol 的 multiplier/tick_size/margin_rate 及其來源
  （`symbol_overrides`/`cost_overrides`/`registry`/`registry (spot
  auto-default)`/`market_default`）；其中一個 symbol 解析失敗不會讓整批
  當掉，只有該筆的 `error` 欄位會填訊息。
- **絕對保證金金額轉比率 — ✅ 已完成**（2026-07-26）。
  `librae.margin_rate_from_absolute(absolute_margin, reference_price,
  multiplier)`（`librae/core/cost_model.py`）機械化了
  `market_config.py` 的 tw_futures 註解裡原本手算的那個換算，交易所調整
  保證金或新增商品時可以直接算，不用重新手動除法。
