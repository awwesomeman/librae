# 2026-03-31 — 資料庫 Schema 現況與優化方向

> 狀態：superseded（P0 tax 欄位已落地，其餘項目移至 04-02）
> 取代者：04-02 db-schema-consolidation
> 更新 2026-04-01：補充 TrendMaster 實驗中發現的實際問題（schema 同步、params 缺失、OHLCV 重複寫入）
> 注記：P0 tax 欄位已由 deploy/migrations/v1_0_0_tax.sql 落地。params JSONB、OHLCV 去重、trade_blotter 索引、signals FK 等項目全部被 04-02 consolidation 吸收並重新規劃

## 現況概覽

- **資料庫**：TimescaleDB (PostgreSQL 16 + timescaledb extension)
- **連線方式**：psycopg2 直連，SimpleConnectionPool (min=1, max=5)
- **Schema 管理**：手動 SQL (`deploy/timescale_init.sql`)，無 ORM、無 migration 工具
- **Schema 版本**：`SCHEMA_VERSION = "1.0.0"` 寫入 `backtest_runs.schema_version`

## 表結構 (6 張)

```
backtest_runs  ← 中心表 (PK: run_id)
  ├── equity_curve         (hypertable, 1M chunks, FK→run_id CASCADE)
  ├── trade_blotter        (普通表, PK: trade_id, FK→run_id CASCADE)
  ├── strategy_signals     (hypertable, 1M chunks, 無 FK)
  ├── strategy_performance (1 row/run, PK=FK: run_id CASCADE)
  └── ohlcv                (hypertable, 1M chunks, 無 FK)
```

### backtest_runs — Run 元資料

| Column | Type | 備註 |
|--------|------|------|
| run_id | TEXT PK | 唯一識別 |
| strategy, symbol, timeframe | TEXT NOT NULL | 策略三元組 |
| sample, data_source | TEXT | 可選 |
| start_ts, end_ts, run_ts | TIMESTAMPTZ | run_ts DEFAULT NOW() |
| schema_version | TEXT | 向後相容檢查 |
| mode | TEXT DEFAULT 'backtest' | backtest / sim / live |
| poll_interval | INTEGER | sim/live 輪詢秒數 |
| last_heartbeat | TIMESTAMPTZ | sim/live 存活檢測 |

### equity_curve — 權益曲線 (hypertable)

| Column | Type |
|--------|------|
| ts | TIMESTAMPTZ NOT NULL |
| run_id | TEXT NOT NULL FK |
| equity, benchmark_equity | DOUBLE PRECISION |
| drawdown, ret_1d, benchmark_ret_1d | DOUBLE PRECISION |

索引：`(run_id, ts DESC)`, 唯一約束：`(ts, run_id)`

### trade_blotter — 交易明細

| Column | Type | 備註 |
|--------|------|------|
| trade_id | TEXT PK | |
| run_id | TEXT NOT NULL FK | |
| entry_ts, exit_ts | TIMESTAMPTZ | |
| symbol, side | TEXT | side: long/short |
| entry_price, exit_price, quantity | DOUBLE PRECISION | |
| gross_pnl, net_pnl | DOUBLE PRECISION | 毛利 vs 淨利 |
| gross_return, net_return | DOUBLE PRECISION | |
| price_unit, quantity_unit, pnl_unit | TEXT | 多幣種支援 |
| commission, slippage | DOUBLE PRECISION DEFAULT 0 | |
| holding_bars | INTEGER | |

### strategy_signals — 策略訊號 (hypertable)

| Column | Type | 備註 |
|--------|------|------|
| ts | TIMESTAMPTZ NOT NULL | |
| run_id | TEXT NOT NULL | 無 FK |
| strategy, symbol, timeframe | TEXT | |
| signal_type | TEXT | entry / exit / hold |
| source | TEXT | backtest / live / sim |
| price, signal_strength, confidence, quantity | DOUBLE PRECISION | |

索引：`(run_id, ts DESC)`, 唯一約束：`(ts, run_id, symbol, signal_type)`

### strategy_performance — 績效摘要 (1 row/run)

| Column | Type |
|--------|------|
| run_id | TEXT PK FK CASCADE |
| total_return, annual_return | DOUBLE PRECISION |
| sharpe, sortino, calmar | DOUBLE PRECISION |
| max_drawdown, win_rate, profit_factor | DOUBLE PRECISION |
| trades | INTEGER |
| avg_trade_return, exposure_ratio | DOUBLE PRECISION |
| benchmark_return | DOUBLE PRECISION |
| total_commission, total_slippage | DOUBLE PRECISION DEFAULT 0 |

### ohlcv — 市場行情 (hypertable)

| Column | Type |
|--------|------|
| ts | TIMESTAMPTZ NOT NULL |
| symbol, timeframe | TEXT NOT NULL |
| run_id | TEXT | 可選 |
| source | TEXT | backtest / live |
| open, high, low, close, volume | DOUBLE PRECISION |

索引：`(symbol, timeframe, ts DESC)`, 唯一約束：`(ts, symbol, timeframe, run_id)`

## 寫入模式

| 場景 | 策略 | 說明 |
|------|------|------|
| Backtest | DELETE old → batch INSERT | `execute_values()` page_size 500-2000，idempotent re-run |
| Live/Sim | ON CONFLICT DO NOTHING / DO UPDATE | 單筆 upsert，不覆蓋已確認資料 |

## 已符合最佳實踐的設計

1. **TIMESTAMPTZ** — 所有時間欄位，確保 UTC 安全
2. **Hypertable 分 chunk** — 高頻時序資料 (equity_curve, signals, ohlcv) 月分 chunk，查詢與壓縮效率佳
3. **Idempotent writes** — backtest DELETE+INSERT / live ON CONFLICT，重跑不產生髒資料
4. **參數化查詢** — `%s` placeholder，無 SQL injection 風險
5. **外鍵 CASCADE** — 刪 run 自動清理下游，referential integrity 完整
6. **成本透明** — gross/net 分離，commission/slippage 獨立追蹤
7. **多市場單位** — price_unit / quantity_unit / pnl_unit 支援跨幣種
8. **Connection pool + context manager** — 自動 commit/rollback/putconn

## 待優化方向

### P0 — 實際遇到的問題

> 以下問題在 TrendMaster 實驗（2026-04-01）中實際觸發，非理論推演。

| 項目 | 現況 | 影響 | 建議 |
|------|------|------|------|
| Schema migration 無自動化 | `timescale_init.sql` 加了 `tax` / `total_tax` 欄位，但 VPS 已存在的 DB 不會自動更新 | `write_backtest_output` 直接報錯 `column "tax" does not exist`，需手動 ALTER TABLE 才能寫入 | 加 `db/migrate.py`，用 `ALTER TABLE ADD COLUMN IF NOT EXISTS` 確保欄位齊全；writer 啟動前自動執行 |
| backtest_runs 不存參數 | DB 有 run_id 和 metrics，但不知道該次 run 用了什麼參數 | Grafana 上只能看曲線，無法比較不同配置（例：MA(9,21) vs MA(9,26) 的 equity 差異） | `backtest_runs` 加 `params JSONB` 欄位，寫入完整 config dict；Grafana 用 `params->>'key'` 篩選 |
| OHLCV 每次 run 重複寫入 | 同一段 BTCUSDT H1 跑 4 次 → 4 × 13K = 52K 行重複 | 儲存浪費、查詢變慢 | ohlcv unique key 改為 `(ts, symbol, timeframe)`（移除 run_id），寫入改 `INSERT ON CONFLICT DO NOTHING` |

### P1 — 短期可改善

| 項目 | 現況 | 建議 | 原因 |
|------|------|------|------|
| trade_blotter 缺 run_id 索引 | 僅 PK(trade_id) | `CREATE INDEX ON trade_blotter(run_id)` | 按 run 查交易是高頻操作，目前走 seq scan |
| TEXT 欄位無約束 | mode, side, signal_type 皆 TEXT | 加 `CHECK` 約束 (e.g. `side IN ('long','short')`) | 防止髒資料寫入 |
| strategy_signals 無 FK | run_id 無 FK 約束 | 加 `REFERENCES backtest_runs(run_id) ON DELETE CASCADE` | 與其他表一致，避免孤兒資料 |
| equity_curve 缺 strategy_name | 只有 run_id，無法直接辨識策略 | 加 `strategy_name TEXT`，或 Grafana JOIN backtest_runs | 方便在 Grafana 疊多條 equity curve 做 overlay 比較 |

### P2 — 中期規模化

| 項目 | 現況 | 建議 | 原因 |
|------|------|------|------|
| ohlcv 綁 run_id | 每次 run 重複存同一段行情 | 見 P0 OHLCV 去重方案 | 避免重複儲存，節省空間 |
| SimpleConnectionPool | 不支援多執行緒 | 若需多 worker 併發寫入，換 `ThreadedConnectionPool` | 當前單執行緒無問題，併發時會出錯 |
| trade_blotter 非 hypertable | 普通表 | 若單策略交易量超過數十萬筆，轉 hypertable | 利用 TimescaleDB 壓縮和分區查詢優化 |

### P3 — 長期演進

| 項目 | 現況 | 建議 | 原因 |
|------|------|------|------|
| 手動 SQL migration | `timescale_init.sql` + schema_version | 引入 Alembic 或 migrate 工具 | 表結構變更頻繁時需要可追蹤的 migration history |
| 無 TimescaleDB 壓縮策略 | chunk 僅按月建立 | 對歷史 chunk 啟用 `ALTER TABLE ... SET (timescaledb.compress)` | 冷資料壓縮可節省 5-10x 儲存 |
| 無 retention policy | 所有資料永久保留 | 對 ohlcv 等可重建資料設定自動 drop policy | 避免儲存無限增長 |

## 相關決策

- [2026-03-06 核心決策整理](2026-03-06-core-tooling-and-schema.md) — Schema 命名規範 (snake_case)
- [2026-03-30 TSDB bind 可配置](2026-03-30-tsdb-bind-configurable.md) — 部署彈性
- [2026-04-01 回測引擎優化](2026-04-01-backtest-engine-optimization.md) — 引擎層優化（SL/TP、cache、效能）
