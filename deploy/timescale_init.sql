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
    strategy            TEXT
);
SELECT create_hypertable('equity_curve', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_equity_curve_run_id ON equity_curve(run_id, ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_equity_curve_unique ON equity_curve(run_id, ts);

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
    trades          INTEGER,
    avg_trade_return DOUBLE PRECISION,
    exposure_ratio  DOUBLE PRECISION,
    benchmark_return DOUBLE PRECISION,
    total_commission DOUBLE PRECISION DEFAULT 0,
    total_slippage  DOUBLE PRECISION DEFAULT 0,
    total_tax       DOUBLE PRECISION DEFAULT 0
);

-- ============================================================
-- ohlcv — 共用市場資料 (hypertable)
-- ============================================================
CREATE TABLE IF NOT EXISTS ohlcv (
    ts              TIMESTAMPTZ NOT NULL,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    data_source     TEXT NOT NULL,
    open            DOUBLE PRECISION,
    high            DOUBLE PRECISION,
    low             DOUBLE PRECISION,
    close           DOUBLE PRECISION,
    volume          DOUBLE PRECISION
);
SELECT create_hypertable('ohlcv', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol ON ohlcv(symbol, timeframe, data_source, ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ohlcv_unique ON ohlcv (ts, symbol, timeframe, data_source);

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
CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_events_unique
    ON signal_events (ts, strategy, symbol, mode, timeframe, signal_type);
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
    range_started_at     TIMESTAMPTZ NOT NULL,
    range_ended_at       TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_coverage_ranges_lookup
    ON ohlcv_coverage_ranges(symbol, timeframe, data_source, range_started_at);

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
    source          TEXT NOT NULL,
    value           DOUBLE PRECISION NOT NULL
);
SELECT create_hypertable('external_factors', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_external_factors_lookup ON external_factors(symbol, factor_name, source, ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_external_factors_unique ON external_factors (ts, symbol, factor_name, source);

-- ============================================================
-- external_factor_coverage_ranges — get_factor() cache 覆蓋區間追蹤
-- (非 hypertable，跟 ohlcv_coverage_ranges 同一種設計)
-- ============================================================
CREATE TABLE IF NOT EXISTS external_factor_coverage_ranges (
    id              SERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL,
    factor_name     TEXT NOT NULL,
    source          TEXT NOT NULL,
    range_started_at     TIMESTAMPTZ NOT NULL,
    range_ended_at       TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_external_factor_coverage_ranges_lookup
    ON external_factor_coverage_ranges(symbol, factor_name, source, range_started_at);
