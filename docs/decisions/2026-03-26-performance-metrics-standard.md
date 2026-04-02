# 2026-03-26 — 績效指標計算標準

> 狀態：accepted（部分待實作）
> ⚠️ 內文提及 InfluxDB 已不適用，DB 已遷移至 TimescaleDB，但計算層分工原則（trade-based metrics.py + quantstats equity-based）仍成立

## 問題背景

現有 `quant_lab/backtest/metrics.py` 的 Sharpe/Sortino 用「trade returns」算，
和業界標準（quantstats、TradingView、券商）對比數字會對不上。

## 決策

### 計算層分工

| 層 | 工具 | 用途 |
|----|------|------|
| InfluxDB 寫入 / 內部邏輯 | 現有 `metrics.py`（trade-based） | 可插拔 registry，方便擴充 |
| 對外展示 / 報告標準 | `quantstats`（equity curve-based） | 和業界標準一致 |

### 為何需要兩套

- `metrics.py`：現有 InfluxDB schema 已依賴，不能大改
- quantstats：業界標準（Sharpe 從時序 bar returns 算，年化因子固定）；輸出 HTML tearsheet

### Active Period 問題（已知，暫緩）

**問題**：策略不是每天都有交易，空倉期的「0 return」被算進 Sharpe 分母，導致 Sharpe 偏低。

**正確做法**（active period Sharpe）：
- 只取有部位的 bar returns 計算 std
- 年化因子用「active bars per year」而非全期

**目前決策**：暫不實作，等策略有完整 OOS 結果後再做。

### 待實作規格（下一版）

1. 在 `metrics.py` 加 `compute_tearsheet(output) -> str`，呼叫 quantstats 產生 HTML 報告
2. 現有指標另存 `active_sharpe`、`active_sortino`（標記 `(active period only)`）
3. 預設展示用 quantstats 數字，active period 指標加 flag 可選

### 年化因子規範（統一）

| 資料頻率 | 年化因子 |
|---------|---------|
| 日頻 | 252 |
| H1 | 365 × 24 = 8760 |
| M60 | 8760 |
| 自訂 | 在 params 明確傳入，不 hardcode |

## 不採用的方案

- 直接改 metrics.py 對齊 quantstats：InfluxDB schema 已依賴現有欄位，改動風險高
- 全部換 quantstats：缺乏 pluggable registry，難以擴充自訂指標
