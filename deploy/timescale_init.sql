-- TimescaleDB Schema v1.1.0
-- For fresh deployments. Existing DBs use deploy/migrations/v1_1_0_consolidation.sql
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============================================================
-- backtest_runs — Run 中樞 (1 row / run)
-- ============================================================
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id          TEXT PRIMARY KEY,
    strategy        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    sample          TEXT,
    data_source     TEXT,
    start_ts        TIMESTAMPTZ,
    end_ts          TIMESTAMPTZ,
    run_ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    schema_version  TEXT,
    mode            TEXT DEFAULT 'backtest',
    poll_interval   INTEGER,
    last_heartbeat  TIMESTAMPTZ,
    params          JSONB,
    CONSTRAINT chk_mode CHECK (mode IN ('backtest', 'sim', 'live'))
);

-- ============================================================
-- equity_curve — 每 bar 淨值 (hypertable)
-- ============================================================
CREATE TABLE IF NOT EXISTS equity_curve (
    ts                  TIMESTAMPTZ NOT NULL,
    run_id              TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    equity              DOUBLE PRECISION,
    benchmark_equity    DOUBLE PRECISION,
    drawdown            DOUBLE PRECISION,
    ret_1d              DOUBLE PRECISION,
    benchmark_ret_1d    DOUBLE PRECISION,
    strategy_name       TEXT
);
SELECT create_hypertable('equity_curve', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_equity_curve_run_id ON equity_curve(run_id, ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_equity_curve_unique ON equity_curve(run_id, ts);

-- ============================================================
-- trade_blotter — 成交記錄 (1 row / trade)
-- ============================================================
CREATE TABLE IF NOT EXISTS trade_blotter (
    trade_id        TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    entry_ts        TIMESTAMPTZ,
    exit_ts         TIMESTAMPTZ,
    symbol          TEXT,
    side            TEXT,
    entry_price     DOUBLE PRECISION,
    exit_price      DOUBLE PRECISION,
    quantity        DOUBLE PRECISION,
    gross_pnl       DOUBLE PRECISION,
    net_pnl         DOUBLE PRECISION,
    gross_return    DOUBLE PRECISION,
    net_return      DOUBLE PRECISION,
    price_unit      TEXT DEFAULT 'USDT',
    quantity_unit   TEXT DEFAULT 'asset',
    pnl_unit        TEXT DEFAULT 'USDT',
    commission      DOUBLE PRECISION DEFAULT 0,
    slippage        DOUBLE PRECISION DEFAULT 0,
    tax             DOUBLE PRECISION DEFAULT 0,
    holding_bars    INTEGER,
    CONSTRAINT chk_side CHECK (side IN ('long', 'short'))
);
CREATE INDEX IF NOT EXISTS idx_trade_blotter_run_id ON trade_blotter(run_id);

-- ============================================================
-- strategy_performance — 聚合 KPI (1 row / run)
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
    run_id          TEXT,           -- optional, no FK (shared market data)
    source          TEXT,
    open            DOUBLE PRECISION,
    high            DOUBLE PRECISION,
    low             DOUBLE PRECISION,
    close           DOUBLE PRECISION,
    volume          DOUBLE PRECISION
);
SELECT create_hypertable('ohlcv', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol ON ohlcv(symbol, timeframe, ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ohlcv_unique ON ohlcv (ts, symbol, timeframe);

-- ============================================================
-- signal_outcomes — 訊號品質監控 (hypertable)
-- ============================================================
CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_ts       TIMESTAMPTZ NOT NULL,
    strategy        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    source          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    signal_value    DOUBLE PRECISION NOT NULL,
    price           DOUBLE PRECISION,
    CONSTRAINT chk_signal_source CHECK (source IN ('backtest', 'sim', 'live'))
);
SELECT create_hypertable('signal_outcomes', 'signal_ts', if_not_exists => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_outcomes_unique
    ON signal_outcomes (signal_ts, strategy, symbol, source, timeframe);
CREATE INDEX IF NOT EXISTS idx_signal_outcomes_lookup
    ON signal_outcomes (strategy, symbol, source, signal_ts DESC);
