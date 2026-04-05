# 2026-04-04 — Order Detail 面板設計調研

> 狀態：research
> 來源：調研主流回測/交易平台的交易明細呈現方式，作為 Grafana Strategy Dashboard 面板改版依據

## 背景

引擎已支援加碼（scaling）與部分平倉（partial close），需要重新設計交易明細面板。
調研各平台如何呈現交易生命週期資料（進場、加碼、部分平倉、全平），以決定面板結構。

---

## 各平台調研

### 1. TradingView — Strategy Tester

**結構**：1 張表，每行 = 一筆完整交易（entry + exit pair）

**欄位**：Trade #, Type (Long/Short), Signal, Entry Date/Price, Exit Date/Price, Contracts, Profit (金額 + %), Cumulative Profit, Run-up, Drawdown

**加碼處理**：開啟 pyramiding 時，每筆進出場都是獨立的一行。不會把多次 scale-in 合併成一個 position。

**特色**：Run-up / Drawdown 是多數平台沒有的——顯示交易期間的最大未實現利潤和虧損。

**結論**：最簡單的單表模型，不區分事件與交易。

- 參考：[List of Trades Tab](https://www.tradingview.com/support/solutions/43000681737-list-of-trades-tab/)
- 參考：[Strategy Tester Report](https://www.tradingview.com/blog/en/changes-in-the-backtesting-report-16416/)

---

### 2. QuantConnect (LEAN)

**結構**：2 個 tab — **Orders** + **Trades**，各自獨立但可展開看子事件

**Orders Tab 欄位**：id, symbol, type (Market/Limit/Stop), direction (Buy/Sell), status, quantity, price, limitPrice, stopPrice, time, tag, value, orderFee

**Order Events**（展開後）：orderEventId, status, fillPrice, fillQuantity, direction, message, time, orderFee

**Trades Tab**：完整交易，可展開看組成的 order fills

**加碼/部分成交處理**：明確建模。一個 Order 可包含多個 OrderEvent（部分成交），各有獨立 fillPrice、fillQuantity、timestamp。

**結論**：業界最成熟的雙層模型，事件級 + 交易級分離，且用展開行而非兩張獨立表。

- 參考：[Results Documentation](https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/results)
- 參考：[Orders API Reference](https://www.quantconnect.com/docs/v2/cloud-platform/api-reference/backtest-management/read-backtest/orders)

---

### 3. Backtrader

**結構**：無內建 UI，純 Python 物件

**Trade 物件**：ref, status (Created/Open/Closed), size, price, value, commission, pnl, pnlcomm, baropen/barclose, barlen, history（事件列表）

**TradeAnalyzer**：聚合統計（total/streak/pnl/won/lost/long/short/length）

**加碼處理**：position 從 0→X 算 open，回到 0 算 close。scaling 修改 size，記錄在 history list。

**結論**：Trade 物件自帶 history，等同事件日誌內嵌於交易物件中。

- 參考：[Trade Documentation](https://www.backtrader.com/docu/trade/)
- 參考：[TradeAnalyzer Reference](https://www.backtrader.com/docu/analyzers-reference/)

---

### 4. Zipline / Quantopian (pyfolio)

**結構**：純 DataFrame，無內建 dashboard

**Transactions DataFrame**：datetime (index), amount (帶正負號), price, symbol

**Positions DataFrame**：datetime (index), 每個 symbol 一欄（持倉市值）, cash

**加碼處理**：每筆成交都是獨立一行。沒有「交易」分組概念。後來 pyfolio 加了 `create_round_trip_tear_sheet()` 才有 round-trip 交易分析。

**結論**：純事件流模型，交易級分析是後來疊加的。

- 參考：[Zipline API Reference](https://zipline.ml4trading.io/appendix.html)
- 參考：[pyfolio Zipline Example](https://pyfolio.ml4trading.io/notebooks/zipline_algo_example.html)

---

### 5. MetaTrader 5

**結構**：3-4 層切換視角 — **Orders / Deals / Positions**

**Deals 欄位**：Time, Deal (ticket), Symbol, Type (Buy/Sell), Direction (in/out/in-out), Volume, Price, S/L, T/P, Commission, Swap, Profit, Change %

**Orders 欄位**：Order request 級（可能未成交），含 ticket, symbol, type, volume, price, state

**Positions 欄位**：聚合的部位生命週期（進場到出場一行）

**加碼處理**：一個 Order 可產生多個 Deals（部分成交）。多個 Deals 修改一個 Position。三層模型（Order → Deal → Position）是所有平台中最細粒度的。

**結論**：三層模型是交易紀錄的黃金標準，但複雜度也最高。

- 參考：[Orders, Positions and Deals in MT5](https://www.mql5.com/en/articles/211)
- 參考：[MT5 Trading Report](https://www.metatrader5.com/en/terminal/help/trading_advanced/history_report)

---

### 6. Interactive Brokers TWS

**結構**：2 個 tab — **Trades** + **Summary**

**Trades Tab 欄位**：Action (Bought/Sold), Quantity, Underlying, Description, Price, Currency, Exchange, Date, Time, Order ID, Commissions, Comment

**加碼/部分成交處理**：每筆成交獨立一行。部分成交顯示為多行（同 Order ID、不同數量和價格）。Summary 提供聚合。

**特色**：Combo/spread 交易可展開看各 leg。

**結論**：典型的 event + summary 雙層模型，但不做 round-trip 交易分組（那是在 Activity Statement / Flex Report 處理）。

- 參考：[IBKR Trade Log Guide](https://www.ibkrguides.com/traderworkstation/trade-log.htm)
- 參考：[TWS Activity Monitor](https://www.interactivebrokers.com/campus/trading-lessons/tws-activity-monitor/)

---

### 7. Binance

**結構**：2 個區塊 — **Order History** + **Trade History**

**Order History 欄位**：Date, Pair, Type, Side, Average Price, Order Price, Executed/Order Amount, Total, Status (Filled/Canceled/Partially Filled)

**Trade History 欄位**：Time, Pair, Side, Price, Filled, Fee, Total, Role (Maker/Taker)

**加碼處理**：部分成交在 Order History 顯示 Partially Filled 狀態，各筆成交在 Trade History 獨立一行。

**結論**：乾淨的兩層分離（request 級 + fill 級），無 round-trip 分組。

- 參考：[Spot Trading Activity FAQ](https://www.binance.com/en/support/faq/how-to-view-my-spot-trading-activity-048b819aed8a4c35b202cba9f977537a)
- 參考：[Transaction History Export](https://www.binance.com/en/support/faq/how-to-download-spot-trading-transaction-history-statement-e4ff64f2533f4d23a0b3f8f17f510eab)

---

## 總結比較

| 平台 | 表數 | 粒度模型 | 加碼處理 |
|------|------|----------|----------|
| TradingView | 1 | 交易級（entry+exit pair） | 每次進出獨立一行 |
| QuantConnect | 2（可展開） | 事件級 + 交易級 | 展開看 partial fills |
| Backtrader | 程式物件 | Trade 物件 + history list | history 記錄事件流 |
| Zipline/pyfolio | DataFrame | 純事件流 | 每筆成交一行 |
| MetaTrader 5 | 3 層切換 | Order → Deal → Position | 三層完整建模 |
| IB TWS | 2 tab | 成交級 + 聚合 | 每筆成交一行 |
| Binance | 2 區塊 | 委託級 + 成交級 | 部分成交獨立行 |

### 共通欄位（所有平台都有）

Timestamp, Symbol, Direction (Buy/Sell), Quantity, Price, P&L/Profit, Commission/Fee

### 關鍵發現

1. **雙層是業界標準**（事件級 + 交易/聚合級），但兩層應互補而非重複
2. **QuantConnect 用展開行**而非兩張獨立表——同一視覺空間內切換粒度
3. **TradingView 的 Run-up / Drawdown** 是獨特且實用的欄位（交易期間最大未實現利潤/虧損）
4. **MT5 三層模型**最完整但複雜度最高，回測系統不需要 Order 請求級
5. 多數平台的 P&L 直接附在成交/交易行上，不需要切換面板查看

### 對本專案的啟示

回測引擎不需要 MT5 的三層模型（沒有委託/撮合延遲）。
最適合的模型是 **QuantConnect 風格的單表 + close 行內嵌 P&L**：
- 一張 Order Events 表涵蓋完整生命週期
- close 事件行直接帶 net_pnl、return%、entry_ts、holding_bars
- `trade_blotter` DB 表保留供指標計算，但不需要獨立面板

---

## 設計決策

### P&L 配對模型：加權平均法（不需要 Order ID）

引擎使用加權平均進場價，所有 buy 混合成 `avg_entry_price`，每次 close 都對 avg 結算：

```
close 的 P&L = (exit_price - avg_entry_price) × quantity × direction
```

不需要逐筆配對（FIFO/LIFO），也不需要 order_id 來關聯進出場。

複雜場景範例（buy → buy → sell 一點 → buy → sell 全部）：

```
動作              qty   price   avg_entry   pos_qty   realized_pnl
─────────────────────────────────────────────────────────────────
1. buy  10@100    10    100     100.00      10        —
2. buy   5@120     5    120     106.67      15        —
3. sell  3@130     3    130     106.67      12        69.99
4. buy   8@110     8    110     108.00      20        —
5. sell 20@140    20    140     108.00       0        640.00
                                            總損益 = 709.99
```

Position lifecycle 靠 `symbol` + `pos_qty` 變化辨識（0 → N → 0 = 一個完整週期），不需要 ID 配對。

### Sim/Live 擴展策略：分階段

| 階段 | 範圍 | 表 | 用途 |
|------|------|---|------|
| **Phase 1（現在）** | 回測 + sim/live 已成交事件 | `order_events` | P&L 追蹤、部位生命週期 |
| **Phase 2（上 live 時）** | 委託狀態追蹤、券商對帳 | `order_log`（新增） | submitted/filled/rejected、broker_order_id |

Phase 1 不預建 order_id 的理由：
- 回測沒有 broker order ID，硬塞會讓 schema 不自然
- P&L 計算與委託狀態追蹤是不同關注點（QuantConnect 也分 Orders tab vs Trades tab）
- 等 live 需求明確時再加，避免過度設計

### Event 類型命名：B+ 對稱模型

4 種類型，方向無關（多空由 `side` 欄位決定）：

```
open    首次建立部位   pos_qty: 0 → N
add     同方向加碼     pos_qty: N → N+M
reduce  部分縮減部位   pos_qty: N → N-M (M < N)
close   全部結清部位   pos_qty: N → 0
```

CHECK constraint：`event_type IN ('open', 'add', 'reduce', 'close')`

命名考量：
- 棄用 `scale_in / partial_close / full_close`：命名不對稱、冗長
- 棄用 `entry / exit`（2 種）：視覺辨識度低，需看 pos_qty 推斷是首次還是加碼
- B+ 一眼就懂，建倉側（open/add）和平倉側（reduce/close）對稱

### Return 欄位

使用 **Net Return %**（扣除成本後的真實報酬）。Gross return 屬進階成本拆解分析，不放主面板。

### Reason 欄位

保留在 DB 和面板。放最後一欄，不影響核心數據閱讀。策略未填時顯示空白。

### 最終面板結構

單一 Order Events 面板，reduce/close 行內嵌完整交易摘要：

| # | Time | Event | Symbol | Side | Qty | Price | Avg Entry | Pos Qty | Cost | Net P&L | Net Return % | Entry Time | Periods | Reason |
|---|------|-------|--------|------|-----|-------|-----------|---------|------|---------|-------------|------------|---------|--------|
| 1 | 10:00 | open | BTC | long | 10 | 100 | 100.00 | 10 | 1.50 | — | — | — | — | RSI |
| 2 | 11:00 | add | BTC | long | 5 | 120 | 106.67 | 15 | 0.85 | — | — | — | — | momentum |
| 3 | 14:00 | reduce | BTC | long | 8 | 130 | 106.67 | 7 | 1.96 | 178.64 | 20.93% | 10:00 | 4 | TP 50% |
| 4 | 16:00 | close | BTC | long | 7 | 140 | 106.67 | 0 | 1.82 | 226.69 | 30.36% | 10:00 | 6 | reversal |
| 5 | 14:00 | open | ETH | short | 20 | 3500 | 3500.00 | 20 | 10.50 | — | — | — | — | bearish div |
| 6 | 16:00 | add | ETH | short | 10 | 3600 | 3533.33 | 30 | 5.10 | — | — | — | — | breakdown |
| 7 | 19:00 | reduce | ETH | short | 15 | 3400 | 3533.33 | 15 | 6.60 | 1982.50 | 3.74% | 14:00 | 5 | cover half |
| 8 | 22:00 | close | ETH | short | 15 | 3300 | 3533.33 | 0 | 6.45 | 3493.55 | 6.59% | 14:00 | 8 | target hit |

- **open/add**：Entry Time、Periods、Net P&L、Net Return % 顯示 `—`
- **reduce/close**：一行包含完整交易資訊，不需切面板
- Trade Summary 面板移除（`trade_blotter` 保留供指標計算，不建面板）
