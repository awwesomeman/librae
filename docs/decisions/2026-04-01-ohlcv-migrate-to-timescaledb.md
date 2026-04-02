# 2026-04-01 — OHLCV 從檔案快取遷移至 TimescaleDB

> 狀態：superseded（未實作）
> 取代者：04-02 db-schema-consolidation
> 注記：提出新建 market_data 表取代 ohlcv，但完全未落地（market_data 表不存在、data/market_data.py 未建）。04-02 consolidation 改為修改現有 ohlcv 表（移除 run_id），問題分析仍有參考價值

## Context

目前 OHLCV 資料管理有四個問題：
1. **兩套重複 fetcher** — `data/binance.py` 與 `pipeline/fetchers/binance_fetcher.py` 邏輯幾乎相同
2. **兩層不連貫的檔案快取** — Parquet (6hr TTL) + JSON hash (5min TTL)，互不知道對方存在
3. **ohlcv 表綁定 run_id** — 同一根 K 線每次回測都重複寫入，無法獨立查詢市場資料
4. **Live/Sim 無持久化快取** — 每次重啟重新從交易所拉整段 warmup

VPS 上已有 TimescaleDB，應該直接作為 OHLCV 唯一資料源。

---

## New Table: `market_data`

```sql
CREATE TABLE IF NOT EXISTS market_data (
    ts          TIMESTAMPTZ      NOT NULL,
    symbol      TEXT             NOT NULL,   -- 'BTCUSDT', 'MXFR1'
    timeframe   TEXT             NOT NULL,   -- '1h', '5m', '1d'
    source      TEXT             NOT NULL,   -- 'binance_spot', 'binance_futures', 'shioaji'
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    volume      DOUBLE PRECISION NOT NULL DEFAULT 0
);

SELECT create_hypertable('market_data', 'ts',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE);

CREATE UNIQUE INDEX IF NOT EXISTS idx_market_data_unique
    ON market_data (ts, symbol, timeframe, source);

CREATE INDEX IF NOT EXISTS idx_market_data_lookup
    ON market_data (source, symbol, timeframe, ts DESC);

-- 壓縮：超過 7 天自動壓
ALTER TABLE market_data SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'source, symbol, timeframe',
    timescaledb.compress_orderby = 'ts'
);
SELECT add_compression_policy('market_data', INTERVAL '7 days');
```

與舊 `ohlcv` 表差異：**無 run_id**，市場資料是共享資源。

---

## Architecture

```
Exchange APIs (Binance, Shioaji)
         │
         ▼
  data/market_data.py          ← 統一模組：fetch + upsert
    get_ohlcv(symbol, tf, start, end, source)
         │
         ▼
  TimescaleDB (VPS)            ← 單一資料源
    market_data table
         │
         ▼
  In-process dict cache        ← 本地速度層 (同 process 內不重複查)
         │
    ┌────┼────────────┐
    ▼    ▼            ▼
 Backtest  Pipeline  Live/Sim
```

`get_ohlcv()` 邏輯：
1. 查 in-memory cache → hit 就回傳
2. 查 DB 有沒有完整涵蓋 `[start, end]`
3. 有缺口 → 從交易所 API 補抓 → upsert 進 DB
4. 回傳合併結果，寫入 memory cache

**DB 不可用時 fallback**：直接打 API 拿資料，log warning，不中斷流程。

---

## Migration Phases

### Phase 0: 建表（無程式碼變動）
- `timescale_init.sql` 加入 `market_data` 表
- 舊 `ohlcv` 表不動
- 一次性 migration script：讀 `data/cache/*.parquet` → bulk insert 進 `market_data`

### Phase 1: 統一 fetcher 模組
- 建立 `data/market_data.py`，包含 `get_ohlcv()` 公開 API
- 從 `pipeline/fetchers/binance_fetcher.py` 吸收 Binance HTTP 分頁 + retry 邏輯
- 內部 helper：`_fetch_binance_spot()`, `_fetch_binance_futures()`
- Shioaji 先保留原位，透過 `market_data.py` re-export

### Phase 2: 遷移 callers — Backtest
- `strategies/trendpullback/utils.py` → `from data.market_data import get_ohlcv`
- `strategies/trendpullback_m5/utils.py` → 同上
- `experiments/trendmaster/utils.py` → 同上
- `run.py` 移除 `write_ohlcv()` 呼叫（fetch 時已寫入 DB）
- `data/binance.py` 改為 thin wrapper + deprecation warning

### Phase 3: 遷移 callers — Pipeline ETL
- `core_data_sources.py` 的 `fetch_binance_*_klines()` → 改呼叫 `get_ohlcv()`
- 移除 `pipeline/features/cache_store.py`

### Phase 4: 遷移 callers — Live/Sim
- `librae/live/wiring.py` 的 warmup 改用 `get_ohlcv()` 從 DB 拿
- 每根新 K 線即時 upsert 進 `market_data`（不再綁 run_id）
- `CryptoAdapter.fetch_ohlcv()` 保留作即時輪詢用，output 由 wiring 層持久化

### Phase 5: 清理
刪除以下：

| 對象 | 說明 |
|------|------|
| `data/binance.py` | 邏輯已移至 `data/market_data.py`（`resample_ohlcv` 搬到 `data/transforms.py`）|
| `pipeline/fetchers/binance_fetcher.py` | 邏輯已吸收 |
| `pipeline/features/cache_store.py` | JSON 快取不再需要 |
| `data/cache/` 目錄 | Parquet + JSON 快取檔案 |
| DB `ohlcv` 表 | 改用 `market_data` |
| `timescale_writer.write_ohlcv()` | 寫入改由 `market_data.py` 處理 |
| `timescale_reader.load_ohlcv(run_id)` | 改為 `(source, symbol, tf)` 查詢 |

---

## Column Name 過渡

| 來源 | 現有欄名 | 新標準 |
|------|----------|--------|
| `data/binance.py` | `timestamp` | `ts` |
| `pipeline/fetchers` | `timestamp` | `ts` |
| `core_data_sources` | index name | `ts` |

`get_ohlcv()` 統一回傳 `ts` 作為時間欄位。Phase 2 遷移 caller 時一併修正。

---

## Risks & Mitigations

| 風險 | 緩解措施 |
|------|---------|
| VPS 延遲拖慢回測 | In-process dict cache，同參數不重複查 DB |
| DB 斷線無法跑任何流程 | `get_ohlcv()` fallback 直接打 API，log warning |
| 遷移期間 sim 在跑 | Phase 4 最後做，舊 `ohlcv` 表保留到 Phase 5 |
| 丟失現有快取資料 | Phase 0 migration script 先把 parquet 灌進 DB |
| 欄位名 `timestamp` vs `ts` | Thin wrapper 過渡，Phase 2 統一修正 |

---

## Critical Files

| 檔案 | 角色 |
|------|------|
| `data/market_data.py` | **新建** — 統一 fetch + DB 讀寫模組 |
| `deploy/timescale_init.sql` | 加入 `market_data` 表 DDL |
| `db/timescale_writer.py` | 移除 `write_ohlcv()`，新增 `upsert_market_data()` |
| `data/binance.py` | 改為 thin wrapper → 最終刪除 |
| `pipeline/fetchers/binance_fetcher.py` | 吸收 retry 邏輯 → 最終刪除 |
| `pipeline/features/core_data_sources.py` | caller 改呼叫 `get_ohlcv()` |
| `pipeline/features/cache_store.py` | 刪除 |
| `strategies/*/utils.py` | caller 改呼叫 `get_ohlcv()` |
| `strategies/*/run.py` | 移除 `write_ohlcv()` 呼叫 |
| `librae/live/wiring.py` | warmup + 持久化改用 `market_data` |

## Verification

- Phase 1: `get_ohlcv("BTCUSDT", "1h", ...)` 成功從 DB 讀寫，`SELECT count(*) FROM market_data` 確認資料
- Phase 2: `python -m strategies.trendpullback.run backtest` 正常跑完，DB 有資料
- Phase 3: pipeline ETL 呼叫正常，`cache_store.py` 已刪除無 import error
- Phase 4: `python -m strategies.trendpullback.run sim` 正常運行，新 K 線出現在 `market_data`
- Phase 5: `data/cache/` 目錄清空，舊模組刪除，全 test suite pass
