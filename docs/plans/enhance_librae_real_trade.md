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
