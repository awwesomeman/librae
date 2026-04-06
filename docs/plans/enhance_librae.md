# Librae Engine 整合優化（索引）

> 狀態：done
> 範圍：engine, executor, schema
> 建立日期：2026-04-04
> 最後更新：2026-04-08
> 依據：[2026-04-01 回測引擎優化](../decisions/2026-04-01-backtest-engine-optimization.md)

整合 decision doc 中散落的引擎層待辦項目，按相關性分為獨立計畫：

| 計畫 | 範圍 | 依賴 | 狀態 |
|------|------|------|------|
| [Position Lifecycle](enhance_librae_position_lifecycle.md) | short fix, scaling, partial close | 無 | done |
| [Engine Performance](enhance_librae_performance.md) | Context slots, cache trim (look-ahead fix) | 無 | done |

### 未來階段（不在此次）

| 項目 | 來源 | 備註 |
|------|------|------|
| SL/TP/Trailing Stop | [decision #4](../decisions/2026-04-01-backtest-engine-optimization.md) | 依賴 Position Lifecycle（需 partial close） |
| `_precompute_bars` numpy 化 | [decision #2](../decisions/2026-04-01-backtest-engine-optimization.md) | 需先建 pytest-benchmark 基線 |
| `ctx.bars_back(n)` | [decision #5](../decisions/2026-04-01-backtest-engine-optimization.md) | 獨立功能 |
| dry-run 輸出增強 | [decision #6](../decisions/2026-04-01-backtest-engine-optimization.md) | UX |
| Margin model / borrow cost | Position Lifecycle 延伸 | 等需要 TW 股票融券時 |
| `fill_log` DB 表 | Position Lifecycle 延伸 | 等需要完整交易歷程時 |
| Max position size guard | Position Lifecycle 延伸 | 防止策略 bug 造成無限加碼 |
| 效能 benchmark 基線 | decision doc 建議 | pytest-benchmark，優化前後對照 |
