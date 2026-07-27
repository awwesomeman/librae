-- TimescaleDB Schema — fresh deployment (DROP + rebuild)
-- See docs/plans/enhance_db_schema.md for schema evolution history
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============================================================
-- backtest_runs — Run 中樞 (1 row / run)
-- ============================================================
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id          TEXT PRIMARY KEY,
    strategy        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    data_source     TEXT,
    started_at      TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ,
    run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mode            TEXT DEFAULT 'backtest',
    poll_seconds    INTEGER,
    last_heartbeat_at TIMESTAMPTZ,
    params          JSONB,
    perf_params     JSONB,
    config_hash     VARCHAR(32),
    CONSTRAINT chk_mode CHECK (mode IN ('backtest', 'sim', 'live'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_backtest_runs_config_hash
    ON backtest_runs(config_hash) WHERE config_hash IS NOT NULL;

-- ============================================================
-- execution_runtime_state -- atomic sim/live restart checkpoint
-- ============================================================
CREATE TABLE IF NOT EXISTS execution_runtime_state (
    state_key       TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    config_hash     VARCHAR(32) NOT NULL,
    mode            TEXT NOT NULL,
    state           JSONB NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_runtime_mode CHECK (mode IN ('sim', 'live'))
);
CREATE INDEX IF NOT EXISTS idx_execution_runtime_run_id
    ON execution_runtime_state(run_id);

-- Completed orders remain here for audit/idempotency while only unfinished
-- orders stay in the compact runtime checkpoint.
CREATE TABLE IF NOT EXISTS broker_orders (
    state_key       TEXT NOT NULL REFERENCES execution_runtime_state(state_key) ON DELETE CASCADE,
    client_order_id TEXT NOT NULL,
    run_id          TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    broker_order_id TEXT,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    status          TEXT NOT NULL,
    placement_attempted BOOLEAN NOT NULL DEFAULT FALSE,
    requested_quantity DOUBLE PRECISION NOT NULL,
    filled_quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
    filled_notional DOUBLE PRECISION NOT NULL DEFAULT 0,
    commission      DOUBLE PRECISION NOT NULL DEFAULT 0,
    slippage        DOUBLE PRECISION NOT NULL DEFAULT 0,
    tax             DOUBLE PRECISION NOT NULL DEFAULT 0,
    submitted_at    TIMESTAMPTZ NOT NULL,
    executed_at     TIMESTAMPTZ,
    request         JSONB NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (state_key, client_order_id),
    CONSTRAINT chk_broker_order_side CHECK (side IN ('buy', 'sell')),
    CONSTRAINT chk_broker_order_status CHECK (
        status IN ('submitted', 'accepted', 'partial', 'filled', 'cancelled', 'rejected')
    )
);
CREATE INDEX IF NOT EXISTS idx_broker_orders_active
    ON broker_orders(state_key, status, updated_at DESC);

-- ============================================================
-- equity_curve — 每 bar 淨值 (hypertable, FK CASCADE)
-- ============================================================
CREATE TABLE IF NOT EXISTS equity_curve (
    ts                  TIMESTAMPTZ NOT NULL,
    run_id              TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    equity              DOUBLE PRECISION,
    benchmark_equity    DOUBLE PRECISION,
    drawdown            DOUBLE PRECISION,
    period_return       DOUBLE PRECISION,
    benchmark_period_return DOUBLE PRECISION,
    gross_exposure      DOUBLE PRECISION,
    net_exposure        DOUBLE PRECISION,
    concentration       DOUBLE PRECISION,
    turnover            DOUBLE PRECISION,
    strategy            TEXT
);
SELECT create_hypertable('equity_curve', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_equity_curve_run_id ON equity_curve(run_id, ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_equity_curve_unique ON equity_curve(run_id, ts);
ALTER TABLE equity_curve ADD COLUMN IF NOT EXISTS gross_exposure DOUBLE PRECISION;
ALTER TABLE equity_curve ADD COLUMN IF NOT EXISTS net_exposure DOUBLE PRECISION;
ALTER TABLE equity_curve ADD COLUMN IF NOT EXISTS concentration DOUBLE PRECISION;
ALTER TABLE equity_curve ADD COLUMN IF NOT EXISTS turnover DOUBLE PRECISION;

-- ============================================================
-- trade_events — 部位生命週期事件 (hypertable, 獨立)
-- ============================================================
CREATE TABLE IF NOT EXISTS trade_events (
    event_id        TEXT NOT NULL,
    run_id          TEXT,
    strategy        TEXT NOT NULL,
    mode            TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    symbol          TEXT,
    side            TEXT,
    event_type      TEXT,
    fill_quantity   DOUBLE PRECISION,
    price           DOUBLE PRECISION,
    entry_price     DOUBLE PRECISION,
    remaining_quantity DOUBLE PRECISION,
    notional        DOUBLE PRECISION,
    commission      DOUBLE PRECISION NOT NULL DEFAULT 0,
    slippage        DOUBLE PRECISION NOT NULL DEFAULT 0,
    tax             DOUBLE PRECISION NOT NULL DEFAULT 0,
    pnl             DOUBLE PRECISION,
    net_return      DOUBLE PRECISION,
    entry_at        TIMESTAMPTZ,
    periods_held       INTEGER,
    reason          TEXT,
    CONSTRAINT chk_event_side CHECK (side IN ('long', 'short')),
    CONSTRAINT chk_event_type CHECK (event_type IN ('open', 'add', 'reduce', 'close')),
    CONSTRAINT chk_event_mode CHECK (mode IN ('backtest', 'sim', 'live'))
);
SELECT create_hypertable('trade_events', 'ts', if_not_exists => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_events_pk ON trade_events(event_id, ts);
CREATE INDEX IF NOT EXISTS idx_trade_events_run_id ON trade_events(run_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_trade_events_strategy ON trade_events(strategy, mode, symbol, ts DESC);

-- ============================================================
-- strategy_performance — 聚合 KPI (1 row / run, FK CASCADE)
-- ============================================================
CREATE TABLE IF NOT EXISTS strategy_performance (
    run_id          TEXT PRIMARY KEY REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    total_return    DOUBLE PRECISION,
    annual_return   DOUBLE PRECISION,
    sharpe          DOUBLE PRECISION,
    sortino         DOUBLE PRECISION,
    calmar          DOUBLE PRECISION,
    max_drawdown    DOUBLE PRECISION,
    win_rate        DOUBLE PRECISION,
    profit_factor   DOUBLE PRECISION,
    payoff_ratio    DOUBLE PRECISION,
    trades          INTEGER,
    avg_trade_return DOUBLE PRECISION,
    exposure_ratio  DOUBLE PRECISION,
    benchmark_return DOUBLE PRECISION,
    tracking_error  DOUBLE PRECISION,
    information_ratio DOUBLE PRECISION,
    total_turnover  DOUBLE PRECISION,
    average_gross_exposure DOUBLE PRECISION,
    max_gross_exposure DOUBLE PRECISION,
    max_abs_net_exposure DOUBLE PRECISION,
    max_concentration DOUBLE PRECISION,
    total_commission DOUBLE PRECISION DEFAULT 0,
    total_slippage  DOUBLE PRECISION DEFAULT 0,
    total_tax       DOUBLE PRECISION DEFAULT 0
);
-- WHY: existing deployments already ran CREATE TABLE before payoff_ratio existed;
-- IF NOT EXISTS keeps this script idempotent for both fresh and existing DBs.
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS payoff_ratio DOUBLE PRECISION;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS tracking_error DOUBLE PRECISION;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS information_ratio DOUBLE PRECISION;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS total_turnover DOUBLE PRECISION;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS average_gross_exposure DOUBLE PRECISION;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS max_gross_exposure DOUBLE PRECISION;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS max_abs_net_exposure DOUBLE PRECISION;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS max_concentration DOUBLE PRECISION;

-- ============================================================
-- ohlcv — 共用市場資料 (hypertable)
-- ============================================================
-- instrument_type: contract expiry structure, orthogonal to continuous
-- rolling-alias handling (see librae/config/symbols.yaml). Keeps e.g.
-- Binance spot BTCUSDT and a same-named perpetual from silently colliding
-- under the same (symbol, data_source) key.
CREATE TABLE IF NOT EXISTS ohlcv (
    ts              TIMESTAMPTZ NOT NULL,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    -- Caller-selectable axis (register_ohlcv_fetcher) — same symbol
    -- legitimately has multiple valid values (research: 'yahoo', live:
    -- 'ibkr'). Unlike external_factors.source, this IS meant to be chosen
    -- per call, not just a fixed provenance tag.
    data_source     TEXT NOT NULL,
    instrument_type TEXT NOT NULL DEFAULT 'spot',
    open            DOUBLE PRECISION,
    high            DOUBLE PRECISION,
    low             DOUBLE PRECISION,
    close           DOUBLE PRECISION,
    volume          DOUBLE PRECISION,
    CONSTRAINT chk_ohlcv_instrument_type CHECK (
        instrument_type IN ('spot', 'contract_perpetual', 'contract_monthly', 'contract_quarterly')
    )
);
SELECT create_hypertable('ohlcv', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol ON ohlcv(symbol, timeframe, data_source, ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ohlcv_unique ON ohlcv (ts, symbol, timeframe, data_source, instrument_type);

-- ============================================================
-- signal_events — 訊號品質監控 (hypertable, 獨立)
-- ============================================================
CREATE TABLE IF NOT EXISTS signal_events (
    ts              TIMESTAMPTZ NOT NULL,
    run_id          TEXT,
    strategy        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    mode            TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    signal_value    DOUBLE PRECISION NOT NULL,
    price           DOUBLE PRECISION,
    signal_type     TEXT NOT NULL DEFAULT 'entry',
    CONSTRAINT chk_signal_mode CHECK (mode IN ('backtest', 'sim', 'live')),
    CONSTRAINT chk_signal_type CHECK (signal_type IN ('entry', 'exit'))
);
SELECT create_hypertable('signal_events', 'ts', if_not_exists => TRUE);
-- run_id is part of the dedup key so re-writing one run's signals (e.g. a
-- parameter-sweep re-run) can never collide with / silently overwrite
-- another run's rows for the same (ts, strategy, symbol, ...) — same
-- per-run isolation as equity_curve/trade_events. DROP+CREATE (not
-- IF NOT EXISTS) so re-running this script also migrates an
-- already-provisioned DB from the old (pre-run_id) index definition.
DROP INDEX IF EXISTS idx_signal_events_unique;
CREATE UNIQUE INDEX idx_signal_events_unique
    ON signal_events (ts, run_id, strategy, symbol, mode, timeframe, signal_type);
CREATE INDEX IF NOT EXISTS idx_signal_events_run_id ON signal_events(run_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_signal_events_lookup
    ON signal_events (strategy, symbol, mode, ts DESC);

-- ============================================================
-- ohlcv_coverage_ranges — get_ohlcv() cache 覆蓋區間追蹤 (非 hypertable)
-- ============================================================
CREATE TABLE IF NOT EXISTS ohlcv_coverage_ranges (
    id              SERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    data_source     TEXT NOT NULL,
    instrument_type TEXT NOT NULL DEFAULT 'spot',
    range_started_at     TIMESTAMPTZ NOT NULL,
    range_ended_at       TIMESTAMPTZ NOT NULL,
    CONSTRAINT chk_ohlcv_coverage_instrument_type CHECK (
        instrument_type IN ('spot', 'contract_perpetual', 'contract_monthly', 'contract_quarterly')
    )
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_coverage_ranges_lookup
    ON ohlcv_coverage_ranges(symbol, timeframe, data_source, instrument_type, range_started_at);

-- ============================================================
-- external_factors — 通用第三方因子資料 (hypertable)
-- 收「有外部抓取成本」的原始序列（funding rate、open interest 等）；
-- 從 OHLCV 現算的衍生特徵（cross_asset、regime）不進這張表，因為隨時能
-- 重算，不需要 gap-tracking。schema 刻意跟 ohlcv 一致（symbol/ts + 一個
-- long 欄位），新資料源只是新的 factor_name，不需要 migration。
-- ============================================================
CREATE TABLE IF NOT EXISTS external_factors (
    ts              TIMESTAMPTZ NOT NULL,
    symbol          TEXT NOT NULL,
    factor_name     TEXT NOT NULL,
    -- Descriptive provenance tag, NOT a caller-selectable axis like
    -- ohlcv.data_source — fixed 1:1 per factor_name at registration time
    -- (register_factor_fetcher), recorded for audit/debugging only. Don't
    -- expect two rows with the same factor_name and different source.
    source          TEXT NOT NULL,
    instrument_type TEXT NOT NULL DEFAULT 'spot',
    value           DOUBLE PRECISION NOT NULL,
    CONSTRAINT chk_external_factors_instrument_type CHECK (
        instrument_type IN ('spot', 'contract_perpetual', 'contract_monthly', 'contract_quarterly')
    )
);
SELECT create_hypertable('external_factors', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_external_factors_lookup ON external_factors(symbol, factor_name, source, ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_external_factors_unique ON external_factors (ts, symbol, factor_name, source, instrument_type);

-- ============================================================
-- external_factor_coverage_ranges — get_factor() cache 覆蓋區間追蹤
-- (非 hypertable，跟 ohlcv_coverage_ranges 同一種設計)
-- ============================================================
CREATE TABLE IF NOT EXISTS external_factor_coverage_ranges (
    id              SERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL,
    factor_name     TEXT NOT NULL,
    source          TEXT NOT NULL,
    instrument_type TEXT NOT NULL DEFAULT 'spot',
    range_started_at     TIMESTAMPTZ NOT NULL,
    range_ended_at       TIMESTAMPTZ NOT NULL,
    CONSTRAINT chk_external_factor_coverage_instrument_type CHECK (
        instrument_type IN ('spot', 'contract_perpetual', 'contract_monthly', 'contract_quarterly')
    )
);
CREATE INDEX IF NOT EXISTS idx_external_factor_coverage_ranges_lookup
    ON external_factor_coverage_ranges(symbol, factor_name, source, instrument_type, range_started_at);

-- ============================================================
-- factor_registry — 每個 factor_name 的更新頻率（一 factor_name 一行，
-- 不是每筆 fact row 都存一次）。由 register_factor_fetcher() 呼叫時的
-- domain 知識寫死，不是從 ts 間隔統計推算——sync_factor_registry() 寫入。
-- frequency 沿用 librae/core/utils.py 既有的 canonical 字母代碼
-- (M5/H8/D1/W2/MN3 ...)，另加 'IRREGULAR' 給沒有固定格點的真實事件資料
-- (股利、分割)。
-- ============================================================
CREATE TABLE IF NOT EXISTS factor_registry (
    factor_name TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    frequency   TEXT NOT NULL
);

-- ============================================================
-- data_inventory — 「目前收錄哪些資料」的即時清單 (view, 非手動維護)
-- 直接查 ohlcv/external_factors 本身算出來，新增 factor 不用同步更新任何
-- 文件——避免命名/清單 drift（見 2026-07 決策討論：不建 UUID catalog table，
-- 靠 DB 自身當唯一真相）。
--
-- frequency：ohlcv 直接用自己本來就有、規則的 timeframe 欄位；
-- external_factors 沒有等效欄位，改 JOIN factor_registry（domain 知識寫死，
-- 見上面 factor_registry 的註解）——不再用相鄰 ts 統計推算，因為樣本少的
-- factor（例如目前只有 2 筆的 us_short_interest）統計出來的間隔不可靠，
-- 也會隨新資料進來一直變動，不是穩定的描述。
-- ============================================================
CREATE OR REPLACE VIEW data_inventory AS
SELECT
    'ohlcv' AS table_name,
    symbol,
    data_source AS source,
    timeframe AS frequency,
    instrument_type,
    NULL::TEXT AS factor_name,
    count(*) AS rows,
    min(ts) AS start_ts,
    max(ts) AS end_ts
FROM ohlcv
GROUP BY symbol, data_source, timeframe, instrument_type

UNION ALL

SELECT
    'external_factors' AS table_name,
    ef.symbol,
    ef.source,
    fr.frequency,
    ef.instrument_type,
    ef.factor_name,
    count(*) AS rows,
    min(ef.ts) AS start_ts,
    max(ef.ts) AS end_ts
FROM external_factors ef
LEFT JOIN factor_registry fr USING (factor_name)
GROUP BY ef.symbol, ef.source, fr.frequency, ef.instrument_type, ef.factor_name

ORDER BY table_name, symbol, factor_name;
