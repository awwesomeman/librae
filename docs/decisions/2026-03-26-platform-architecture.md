# 2026-03-26 — 平台架構與 Signal Engine 設計

> 狀態：accepted（部分過時）
> 注記：核心分層（signal_engine → vectorbt → bar-by-bar → 執行層）仍成立。執行層描述過時：Lumibot 已被 librae live engine 取代。Signal engine pure function 原則、pandas_ta 統一、warmup 規範仍有效

## 架構總覽（模組化分工）

```
signal_engine（pure function）
    ↓
vectorbt（研究/參數掃描）
    ↓
自建 bar-by-bar runner（高保真回測，含成本/滑價）
    ↓
執行層（Lumibot / ib_insync / CCXT / Shioaji）
```

### 各層職責

| 層級 | 工具 | 職責 |
|------|------|------|
| 核心大腦 | `signal_engine.py`（純 Python/Pandas） | 產出交易訊號，唯一邏輯來源 |
| 研究過濾 | vectorbt | 大規模參數掃描、多 TF 趨勢過濾 |
| 高保真驗證 | 自建 runner | 加入手續費/滑價/DST 處理，訂閱者績效標準 |
| 實盤執行 | 依市場選工具 | 只負責下單、斷線重連、訂單監控 |

### 執行層工具選擇

| 市場 | 工具 | 理由 |
|------|------|------|
| 加密貨幣 | CCXT（免費版先行，Pro 視需求） | 全球標準、維護快 |
| 美股/期貨 | ib_insync | IB 原生支援最穩 |
| 台股/台指 | Shioaji | 國外框架不支援台灣市場 |

---

## Signal Engine 內部架構

### 設計原則

- **Pure function**：無副作用、無 I/O、無時區轉換
- **輸入**：OHLCV DataFrame + params dict
- **輸出**：帶 signal column 的 DataFrame（1=buy, 0=hold, -1=sell）
- **不用 class**，避免隱藏狀態；用 function + params dict

### 四層結構

```
1. 資料清洗層 → 空值處理、時區統一（UTC）、多市場對齊
2. 指標計算層 → pandas_ta 統一庫（全專案不混用 TA-Lib）
3. 訊號邏輯層 → 多時間框架交集、優先級鏈（np.where）
4. 風控過濾層 → 波動率 gate、異常市場過濾
```

### 實作規範

**指標庫統一**：全專案使用 `pandas_ta`，不混用 TA-Lib 或自寫公式。

**多時間框架對齊**：
- D1 訊號必須 `shift(1)`（只用已收盤的值）
- H1 和 D1 用 `pd.merge_asof` 或 index alignment 對齊
- 這是 look-ahead bias 高風險區，必須有自動化測試驗證

**訊號衝突處理**：
```python
signal = np.where(buy_cond, 1, np.where(sell_cond, -1, 0))
```
不用 `df.loc[cond] = 1` 避免覆蓋衝突。

**Warmup 期**：前 N 根 bar（取決於最長指標）標記為 NaN，不進入回測。

**Pure function 範例**：
```python
def compute_indicators(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    out = df.copy()
    out["rsi"] = ta.rsi(out["close"], length=params["rsi_length"])
    return out

def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    out = compute_indicators(df, params)
    buy = (out["close"] > out["ma_slow"]) & (out["rsi"] < 30)
    sell = out["close"] < out["ma_fast"]
    out["signal"] = np.where(buy, 1, np.where(sell, -1, 0))
    return out
```

---

## 尚未納入但需要的（後續迭代）

| 功能 | 說明 | 優先級 |
|------|------|--------|
| Position Sizing | signal 只說買賣，不說買多少；需要獨立的倉位管理層 | 高 |
| Risk Gate | 獨立的風控過濾（波動率/drawdown/市場異常） | 高 |
| Signal Quality Monitor | 即時訊號 vs 回測預期的 drift detection | 中（訂閱平台必要） |
| Slippage Model | `entry_price + N ticks` 保守估計 | 高（目前為 0） |

---

## 深水區防護

| 風險 | 防護 | 狀態 |
|------|------|------|
| DST 陷阱 | 系統內 UTC，自建 runner 已修 | ✅ |
| Look-ahead Bias | D1 shift(1) + 自動化測試 | ⚠️ 有規範，待加測試 |
| 生存者偏誤 | 選股策略須含下市股（BTC 不適用） | 待選股時處理 |
| 執行一致性 | 加 slippage_bps 參數 | ❌ 待實作 |

---

## 不採用的方案

| 方案 | 理由 |
|------|------|
| Lean Engine | 學習成本和遷移成本遠超現階段規模 |
| Lumibot 做回測 | DST 無限迴圈 bug，24/7 crypto 不可用 |
| NautilusTrader | 需要整個技術棧重寫，等資金 10x 後再考慮 |
| 自寫指標公式 | pandas_ta 已有社群驗證，避免重複造輪子 |
