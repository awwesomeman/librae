# Plan: Real Trade Cost Enhancements

Status: **backlog** — 記錄未來需考慮的真實交易成本項目，目前不實作。

## Context

margin_rate 已完成（基本保證金對資本效率的影響），以下是更精細的真實交易成本，
在策略上線真實交易或需要更嚴格的回測模擬時再逐步加入。

---

## A. 借券費與利息 (Borrowing Fees / Rebates)

- 美股/台股做空需支付借券費 (Stock Loan Fee)，通常 annualized 0.3%~數十%
- 保證金留在經紀商處可能產生微薄利息 (Credit Interest)
- 融資買入時有利息支出
- **實作方向**: CostModel 加 `borrow_rate` / `financing_rate`，在 eval_equity 按持倉天數累計

## B. 維持保證金 (Maintenance Margin)

- 持倉期間需監控: Equity = Margin + Unrealized_PnL
- 若 Equity < Maintenance_Margin → 觸發強制平倉 (Margin Call)
- 不考慮此點會過度樂觀地估計策略在劇烈波動下的存活能力
- **實作方向**: CostModel 加 `maintenance_margin_rate`，engine 每 bar 檢查，觸發時產生 forced close Action

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
