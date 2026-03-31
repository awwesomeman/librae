-- TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- 回測 run 索引
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
    poll_interval   INTEGER,          -- seconds between poll cycles (sim/live only)
    last_heartbeat  TIMESTAMPTZ       -- updated every poll cycle (sim/live only)
);

-- equity curve（hypertable，chunk = 1 month）
CREATE TABLE IF NOT EXISTS equity_curve (
    ts                  TIMESTAMPTZ NOT NULL,
    run_id              TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    equity              DOUBLE PRECISION,
    benchmark_equity    DOUBLE PRECISION,
    drawdown            DOUBLE PRECISION,
    ret_1d              DOUBLE PRECISION,
    benchmark_ret_1d    DOUBLE PRECISION
);
SELECT create_hypertable('equity_curve', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_equity_curve_run_id ON equity_curve(run_id, ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_equity_curve_unique ON equity_curve(run_id, ts);

-- trade blotter
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
    holding_bars    INTEGER
);

-- strategy signals（live + backtest, hypertable）
CREATE TABLE IF NOT EXISTS strategy_signals (
    ts              TIMESTAMPTZ NOT NULL,
    run_id          TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    strategy        TEXT,
    symbol          TEXT,
    timeframe       TEXT,
    signal_type     TEXT,   -- entry / exit / hold
    source          TEXT,   -- backtest / live / sim
    price           DOUBLE PRECISION,
    signal_strength DOUBLE PRECISION,
    confidence      DOUBLE PRECISION,
    quantity        DOUBLE PRECISION
);
SELECT create_hypertable('strategy_signals', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_signals_run_id ON strategy_signals(run_id, ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_unique ON strategy_signals(ts, run_id, symbol, signal_type);

-- strategy performance（one row per run）
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

-- ohlcv（hypertable）
CREATE TABLE IF NOT EXISTS ohlcv (
    ts              TIMESTAMPTZ NOT NULL,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    run_id          TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    source          TEXT,   -- backtest / live
    open            DOUBLE PRECISION,
    high            DOUBLE PRECISION,
    low             DOUBLE PRECISION,
    close           DOUBLE PRECISION,
    volume          DOUBLE PRECISION
);
SELECT create_hypertable('ohlcv', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol ON ohlcv(symbol, timeframe, ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ohlcv_unique ON ohlcv (ts, symbol, timeframe, run_id);
