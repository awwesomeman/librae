-- TimescaleDB schema for an empty database or an already-current database.
-- This file never upgrades an older schema; migrating one is the deployment's job.
-- See docs/plans/enhance_db_schema.md for schema evolution history
\set ON_ERROR_STOP on
BEGIN;

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Refuse to stamp an unversioned existing database as current.
DO $$
BEGIN
    IF to_regclass('public.backtest_runs') IS NOT NULL
       AND to_regclass('public.librae_schema_revision') IS NULL THEN
        RAISE EXCEPTION
            'unversioned Librae schema; its deployment must migrate it before this bootstrap runs';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS librae_schema_revision (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO librae_schema_revision (singleton, revision)
VALUES (TRUE, 3)
ON CONFLICT (singleton) DO NOTHING;
DO $$
BEGIN
    IF (SELECT revision FROM librae_schema_revision WHERE singleton = TRUE) <> 3 THEN
        RAISE EXCEPTION 'database schema revision does not match bootstrap revision 3';
    END IF;
END
$$;

-- Managed roles: quant_app writes runtime data; grafana_reader only reads it.
-- The connecting quant role remains reserved for migrations and administration.
\getenv quant_app_password POSTGRES_APP_PASSWORD
\getenv grafana_reader_password POSTGRES_GRAFANA_PASSWORD

SELECT 'CREATE ROLE quant_app'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'quant_app')
\gexec
ALTER ROLE quant_app WITH
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD :'quant_app_password';

SELECT 'CREATE ROLE grafana_reader'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_reader')
\gexec
ALTER ROLE grafana_reader WITH
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD :'grafana_reader_password';

REVOKE ALL PRIVILEGES ON SCHEMA public FROM quant_app, grafana_reader;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM quant_app, grafana_reader;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM quant_app, grafana_reader;
GRANT USAGE ON SCHEMA public TO quant_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO quant_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO quant_app;
GRANT USAGE ON SCHEMA public TO grafana_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_reader;
REVOKE INSERT, UPDATE, DELETE ON librae_schema_revision FROM quant_app, grafana_reader;

ALTER DEFAULT PRIVILEGES FOR ROLE quant IN SCHEMA public
    REVOKE ALL PRIVILEGES ON TABLES FROM quant_app, grafana_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE quant IN SCHEMA public
    REVOKE ALL PRIVILEGES ON SEQUENCES FROM quant_app, grafana_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE quant IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO quant_app;
ALTER DEFAULT PRIVILEGES FOR ROLE quant IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO quant_app;
ALTER DEFAULT PRIVILEGES FOR ROLE quant IN SCHEMA public
    GRANT SELECT ON TABLES TO grafana_reader;

-- ============================================================
-- backtest_runs — Run 中樞 (1 row / run)
-- ============================================================
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id          TEXT PRIMARY KEY,
    strategy_name   TEXT NOT NULL,
    symbols         JSONB NOT NULL,
    timeframe       TEXT NOT NULL,
    data_source     TEXT,
    data_source_by_symbol JSONB NOT NULL DEFAULT '{}'::jsonb,
    primary_subscriptions JSONB NOT NULL,
    session_mode    TEXT NOT NULL DEFAULT 'extended',
    started_at      TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ,
    run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mode            TEXT DEFAULT 'backtest',
    poll_seconds    INTEGER,
    last_heartbeat_at TIMESTAMPTZ,
    params          JSONB,
    execution_policy JSONB,
    risk_policy     JSONB,
    config_hash     VARCHAR(32),
    backtest_revision TEXT,
    backtest_cache_key VARCHAR(32),
    execution_identity JSONB,
    CONSTRAINT chk_mode CHECK (mode IN ('backtest', 'sim', 'live')),
    CONSTRAINT chk_run_data_sources_object
        CHECK (jsonb_typeof(data_source_by_symbol) = 'object'),
    CONSTRAINT chk_primary_subscriptions_array
        CHECK (
            jsonb_typeof(primary_subscriptions) = 'array'
            AND (
                jsonb_array_length(primary_subscriptions) = 0
                OR jsonb_array_length(primary_subscriptions) = jsonb_array_length(symbols)
            )
        ),
    CONSTRAINT chk_session_mode CHECK (session_mode IN ('regular', 'extended')),
    CONSTRAINT chk_execution_identity_object
        CHECK (execution_identity IS NULL OR jsonb_typeof(execution_identity) = 'object')
);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_config_hash
    ON backtest_runs(config_hash) WHERE config_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_backtest_runs_cache_key
    ON backtest_runs(backtest_cache_key) WHERE backtest_cache_key IS NOT NULL;

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
    placement_attempted_at TIMESTAMPTZ,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
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
        status IN (
            'submitted', 'accepted', 'partial', 'cancel_pending',
            'filled', 'cancelled', 'rejected'
        )
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
    account_id          TEXT NOT NULL,
    currency            TEXT NOT NULL,
    equity              DOUBLE PRECISION,
    drawdown            DOUBLE PRECISION,
    period_return       DOUBLE PRECISION,
    gross_exposure      DOUBLE PRECISION,
    net_exposure        DOUBLE PRECISION,
    concentration       DOUBLE PRECISION,
    turnover            DOUBLE PRECISION,
    exposed             BOOLEAN,
    strategy_name       TEXT NOT NULL
);
SELECT create_hypertable('equity_curve', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_equity_curve_run_id ON equity_curve(run_id, ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_equity_curve_unique
    ON equity_curve(run_id, account_id, ts);

-- ============================================================
-- position_events — 部位生命週期事件 (hypertable, 獨立)
-- ============================================================
-- event_type: open/add = entry side, reduce/close = exit side.
-- realized_pnl/net_return/entry_at/periods_held are populated on reduce/close
-- only (computed against the weighted-average entry_price), not on open/add.
-- entry_price = running weighted-average entry basis, not this row's fill
-- price; remaining_quantity = position size AFTER this event (not the
-- fill_quantity of this event).
CREATE TABLE IF NOT EXISTS position_events (
    event_id        TEXT NOT NULL,
    run_id          TEXT,
    strategy_name   TEXT NOT NULL,
    mode            TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    account_id      TEXT NOT NULL,
    currency        TEXT NOT NULL,
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
    entry_commission DOUBLE PRECISION,
    entry_slippage  DOUBLE PRECISION,
    entry_tax       DOUBLE PRECISION,
    realized_pnl    DOUBLE PRECISION,
    net_return      DOUBLE PRECISION,
    entry_at        TIMESTAMPTZ,
    periods_held       INTEGER,
    reason          TEXT,
    group_id        TEXT,
    time_in_force   TEXT,
    margin_locked   DOUBLE PRECISION,
    leverage        DOUBLE PRECISION,
    liquidation_price DOUBLE PRECISION,
    margin_roi      DOUBLE PRECISION,
    -- Who set margin_rate for this fill's side, not how big it is: unlevered
    -- (spot/cash, rate always 1.0), fixed (exchange/regulator-set, e.g.
    -- TAIFEX margin or Reg-T/融資), dynamic (trader-chosen leverage, e.g.
    -- isolated-margin perps). Same leverage number means different things
    -- under each mode — see librae/config/market_config.py's MarginMode.
    margin_mode     TEXT,
    -- Net cash impact of this fill on the account: negative on open/add
    -- (outlay = notional*margin_rate + this row's commission+slippage+tax),
    -- positive on reduce/close (proceeds = released margin + realized PnL -
    -- exit costs). Already nets out commission/slippage/tax — not a second
    -- copy of those columns, the total after them.
    cash_flow       DOUBLE PRECISION,
    CONSTRAINT chk_event_side CHECK (side IN ('long', 'short')),
    CONSTRAINT chk_event_type CHECK (event_type IN ('open', 'add', 'reduce', 'close')),
    CONSTRAINT chk_event_mode CHECK (mode IN ('backtest', 'sim', 'live')),
    CONSTRAINT chk_event_time_in_force CHECK (time_in_force IN ('day', 'gtc', 'ioc', 'fok')),
    CONSTRAINT chk_event_margin_mode CHECK (margin_mode IN ('unlevered', 'fixed', 'dynamic'))
);
SELECT create_hypertable('position_events', 'ts', if_not_exists => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS idx_position_events_pk ON position_events(event_id, ts);
CREATE INDEX IF NOT EXISTS idx_position_events_run_id ON position_events(run_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_position_events_strategy ON position_events(strategy_name, mode, symbol, ts DESC);

-- Timestamped position-financing cash flows applied by research runtimes:
-- perpetual funding settlements and interest on a short's borrowed asset.
CREATE TABLE IF NOT EXISTS financing_cash_flows (
    ts              TIMESTAMPTZ NOT NULL,
    run_id          TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    account_id      TEXT NOT NULL,
    currency        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    side            TEXT NOT NULL,
    quantity        DOUBLE PRECISION NOT NULL,
    mark_price      DOUBLE PRECISION NOT NULL,
    multiplier      DOUBLE PRECISION NOT NULL,
    rate            DOUBLE PRECISION NOT NULL,
    cash_flow       DOUBLE PRECISION NOT NULL,
    group_id        TEXT,
    entry_at        TIMESTAMPTZ NOT NULL,
    CONSTRAINT chk_financing_kind CHECK (kind IN ('funding', 'borrow')),
    CONSTRAINT chk_financing_side CHECK (side IN ('long', 'short')),
    CONSTRAINT chk_financing_quantity CHECK (quantity > 0),
    CONSTRAINT chk_financing_mark_price CHECK (mark_price > 0),
    CONSTRAINT chk_financing_multiplier CHECK (multiplier > 0)
);
SELECT create_hypertable('financing_cash_flows', 'ts', if_not_exists => TRUE);
-- kind belongs in the key: a funding settlement and a borrow accrual land on
-- the same symbol at the same ts and must not overwrite each other.
CREATE UNIQUE INDEX IF NOT EXISTS idx_financing_cash_flows_unique
    ON financing_cash_flows(run_id, account_id, symbol, kind, ts);
CREATE INDEX IF NOT EXISTS idx_financing_cash_flows_run_id
    ON financing_cash_flows(run_id, account_id, ts DESC);

-- ============================================================
-- runtime_events — operational audit trail (restarts, skipped decisions),
-- not a fill. event_type is deliberately small; extend it only when a new
-- call site actually needs to report one.
-- ============================================================
CREATE TABLE IF NOT EXISTS runtime_events (
    ts              TIMESTAMPTZ NOT NULL,
    run_id          TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    symbol          TEXT,
    detail          JSONB,
    CONSTRAINT chk_runtime_event_type
        CHECK (event_type IN ('state_recovered', 'decision_skipped'))
);
SELECT create_hypertable('runtime_events', 'ts', if_not_exists => TRUE);
-- COALESCE(symbol, '') so multiple NULL-symbol events (state_recovered,
-- batch-level rebalance skips) still dedupe on retry — plain NULL columns
-- never collide with each other under a standard unique index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_events_unique
    ON runtime_events(run_id, ts, event_type, COALESCE(symbol, ''));
CREATE INDEX IF NOT EXISTS idx_runtime_events_run_id ON runtime_events(run_id, ts DESC);

-- ============================================================
-- strategy_performance — 帳戶 KPI (1 row / account / run, FK CASCADE)
-- ============================================================
CREATE TABLE IF NOT EXISTS strategy_performance (
    run_id          TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    account_id      TEXT NOT NULL,
    currency        TEXT NOT NULL,
    initial_cash    DOUBLE PRECISION NOT NULL,
    final_equity    DOUBLE PRECISION NOT NULL,
    net_pnl         DOUBLE PRECISION NOT NULL,
    total_return    DOUBLE PRECISION,
    mean_period_return DOUBLE PRECISION,
    period_volatility DOUBLE PRECISION,
    period_downside_deviation DOUBLE PRECISION,
    period_sharpe   DOUBLE PRECISION,
    period_sortino  DOUBLE PRECISION,
    positive_period_rate DOUBLE PRECISION,
    max_drawdown    DOUBLE PRECISION,
    win_rate        DOUBLE PRECISION,
    profit_factor   DOUBLE PRECISION,
    payoff_ratio    DOUBLE PRECISION,
    trades          INTEGER,
    avg_trade_return DOUBLE PRECISION,
    exposure_ratio  DOUBLE PRECISION,
    total_turnover  DOUBLE PRECISION,
    average_gross_exposure DOUBLE PRECISION,
    max_gross_exposure DOUBLE PRECISION,
    max_abs_net_exposure DOUBLE PRECISION,
    max_concentration DOUBLE PRECISION,
    total_commission DOUBLE PRECISION DEFAULT 0,
    total_slippage  DOUBLE PRECISION DEFAULT 0,
    total_tax       DOUBLE PRECISION DEFAULT 0,
    PRIMARY KEY (run_id, account_id)
);

-- ============================================================
-- instrument_type — one definition for every table that stores it.
-- Adding a new instrument type (e.g. options) is a single edit here;
-- keep librae/config/symbols.py's ALLOWED_INSTRUMENT_TYPES in step.
-- ============================================================
-- CREATE DOMAIN has no IF NOT EXISTS; the DO block keeps re-runs idempotent
-- (with ON_ERROR_STOP a bare duplicate aborts everything after this line).
DO $$ BEGIN
    CREATE DOMAIN instrument_type_t AS TEXT
        CONSTRAINT chk_instrument_type
        CHECK (VALUE IN ('spot', 'contract_perpetual', 'contract_monthly', 'contract_quarterly'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ============================================================
-- symbols — instrument master. The fact tables store `symbol` as a bare
-- string; this is what that string means. Keyed by the same
-- (symbol, data_source, instrument_type) triple the fact tables already
-- carry, so it joins to ohlcv/external_factors without touching them --
-- deliberately no foreign keys: a hypertable FK costs a lookup per inserted
-- row, and reference data arriving after the facts is normal, not an error.
--
-- Populated from librae.config.symbols' registry via write_symbols(). The
-- YAML is a deployment-time file; the database is shared across
-- deployments, so data that lands here without its identity is data nobody
-- else can interpret.
--
-- Columns mirror SymbolInfo exactly. Attributes for instrument types librae
-- does not model yet (an option's strike/expiry/right) belong with whatever
-- adds that type and the SymbolInfo fields to populate them -- an empty
-- column is worse than an absent one.
-- ============================================================
CREATE TABLE IF NOT EXISTS symbols (
    symbol           TEXT NOT NULL,
    data_source      TEXT NOT NULL,
    instrument_type  instrument_type_t NOT NULL,
    market           TEXT NOT NULL,
    multiplier       DOUBLE PRECISION NOT NULL,
    data_adapter     TEXT NOT NULL,
    venue_symbol     TEXT NOT NULL,
    currency         TEXT NOT NULL,
    continuous_alias BOOLEAN NOT NULL DEFAULT FALSE,
    contract_month   TEXT,
    tick_size        DOUBLE PRECISION,
    security_type    TEXT,
    exchange         TEXT,
    calendar_id      TEXT,
    quantity_step    DOUBLE PRECISION,
    min_quantity     DOUBLE PRECISION,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_symbols PRIMARY KEY (symbol, data_source, instrument_type),
    CONSTRAINT chk_symbols_multiplier CHECK (multiplier > 0),
    CONSTRAINT chk_symbols_tick_size CHECK (tick_size IS NULL OR tick_size > 0),
    CONSTRAINT chk_symbols_quantity_step CHECK (quantity_step IS NULL OR quantity_step > 0),
    CONSTRAINT chk_symbols_min_quantity CHECK (min_quantity IS NULL OR min_quantity > 0)
);
CREATE INDEX IF NOT EXISTS idx_symbols_market ON symbols(market, instrument_type);
CREATE INDEX IF NOT EXISTS idx_symbols_calendar ON symbols(calendar_id);

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
    -- Caller-selectable axis (register_ohlcv_fetcher) — the same symbol
    -- legitimately has multiple valid values (research: 'yahoo', live:
    -- 'ibkr'), so it is chosen per call. external_factors.data_source
    -- names the same concept but is fixed per factor_name, not selectable.
    data_source     TEXT NOT NULL,
    instrument_type instrument_type_t NOT NULL DEFAULT 'spot',
    calendar_id     TEXT NOT NULL,
    session_mode    TEXT NOT NULL DEFAULT 'extended',
    available_at    TIMESTAMPTZ NOT NULL,
    open            DOUBLE PRECISION,
    high            DOUBLE PRECISION,
    low             DOUBLE PRECISION,
    close           DOUBLE PRECISION,
    volume          DOUBLE PRECISION,
    CONSTRAINT chk_ohlcv_session_mode CHECK (session_mode IN ('regular', 'extended')),
    CONSTRAINT chk_ohlcv_available_at CHECK (available_at >= ts)
);
SELECT create_hypertable('ohlcv', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol
    ON ohlcv(
        symbol, timeframe, calendar_id, session_mode, data_source, instrument_type, ts DESC
    );
CREATE UNIQUE INDEX IF NOT EXISTS idx_ohlcv_unique
    ON ohlcv (
        ts, symbol, timeframe, calendar_id, session_mode, data_source, instrument_type
    );

-- ============================================================
-- signal_events — 訊號品質監控 (hypertable, 獨立)
-- ============================================================
CREATE TABLE IF NOT EXISTS signal_events (
    ts              TIMESTAMPTZ NOT NULL,
    run_id          TEXT,
    strategy_name   TEXT NOT NULL,
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
-- another run's rows for the same (ts, strategy_name, symbol, ...).
CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_events_unique
    ON signal_events (ts, run_id, strategy_name, symbol, mode, timeframe, signal_type);
CREATE INDEX IF NOT EXISTS idx_signal_events_run_id ON signal_events(run_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_signal_events_lookup
    ON signal_events (strategy_name, symbol, mode, ts DESC);

-- ============================================================
-- ohlcv_coverage_ranges — get_ohlcv() cache 覆蓋區間追蹤 (非 hypertable)
-- ============================================================
CREATE TABLE IF NOT EXISTS ohlcv_coverage_ranges (
    id              SERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    data_source     TEXT NOT NULL,
    instrument_type instrument_type_t NOT NULL DEFAULT 'spot',
    calendar_id     TEXT NOT NULL,
    session_mode    TEXT NOT NULL DEFAULT 'extended',
    range_started_at     TIMESTAMPTZ NOT NULL,
    range_ended_at       TIMESTAMPTZ NOT NULL,
    CONSTRAINT chk_ohlcv_coverage_session_mode
        CHECK (session_mode IN ('regular', 'extended'))
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_coverage_ranges_lookup
    ON ohlcv_coverage_ranges(
        symbol, timeframe, calendar_id, session_mode, data_source, instrument_type,
        range_started_at
    );

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
    -- Observation frequency of THIS row, the factor-family counterpart of
    -- ohlcv.timeframe. In the fact key so one factor can be stored at
    -- several frequencies; packing it into factor_name instead
    -- ('x_1d'/'x_1h') would hide a real dimension inside a string.
    timeframe       TEXT NOT NULL,
    -- Same concept as ohlcv.data_source (which upstream produced this),
    -- but fixed 1:1 per factor_name at registration time
    -- (register_factor_fetcher) rather than chosen per call. Don't expect
    -- two rows with the same factor_name and different data_source.
    data_source     TEXT NOT NULL,
    instrument_type instrument_type_t NOT NULL DEFAULT 'spot',
    -- Deliberately one scalar number. An observation that is non-numeric
    -- (a regime label) or multi-dimensional (a term structure, an option
    -- chain row) gets its OWN fact table with its own columns and
    -- constraints -- it does not get a value_json/value_text sibling here.
    -- Two nullable value columns with no way to require exactly one is the
    -- point where a fact table stops being queryable or checkable, and the
    -- extra dimensions (strike, expiry, tenor) would still have nowhere to
    -- live. Storing them as several factor_names is the same mistake in a
    -- different place.
    value           DOUBLE PRECISION NOT NULL
);
SELECT create_hypertable('external_factors', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_external_factors_lookup ON external_factors(symbol, factor_name, timeframe, data_source, ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_external_factors_unique ON external_factors (ts, symbol, factor_name, timeframe, data_source, instrument_type);

-- ============================================================
-- external_factor_coverage_ranges — get_factor() cache 覆蓋區間追蹤
-- (非 hypertable，跟 ohlcv_coverage_ranges 同一種設計)
-- ============================================================
CREATE TABLE IF NOT EXISTS external_factor_coverage_ranges (
    id              SERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL,
    factor_name     TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    data_source     TEXT NOT NULL,
    instrument_type instrument_type_t NOT NULL DEFAULT 'spot',
    range_started_at     TIMESTAMPTZ NOT NULL,
    range_ended_at       TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_external_factor_coverage_ranges_lookup
    ON external_factor_coverage_ranges(symbol, factor_name, timeframe, data_source, instrument_type, range_started_at);

-- ============================================================
-- factor_registry — 每個 factor_name 的更新頻率 (timeframe)（一 factor_name 一行，
-- 不是每筆 fact row 都存一次）。由 register_factor_fetcher() 呼叫時的
-- domain 知識寫死，不是從 ts 間隔統計推算——sync_factor_registry() 寫入。
-- timeframe 沿用 librae/core/utils.py 既有的 canonical 字母代碼
-- (M5/H8/D1/W2/MN3 ...)，另加 'IRREGULAR' 給沒有固定格點的真實事件資料
-- (股利、分割)。
-- ============================================================
-- timeframe here is the frequency a factor is REGISTERED at, which a factor
-- with no rows yet still has; external_factors.timeframe is what each stored
-- row actually is. They normally agree.
CREATE TABLE IF NOT EXISTS factor_registry (
    factor_name TEXT PRIMARY KEY,
    data_source TEXT NOT NULL,
    timeframe   TEXT NOT NULL
);

-- ============================================================
-- data_inventory — 「目前收錄哪些資料」的即時清單 (view, 非手動維護)
-- 直接查 ohlcv/external_factors 本身算出來，新增 factor 不用同步更新任何
-- 文件——避免命名/清單 drift（見 2026-07 決策討論：不建 UUID catalog table，
-- 靠 DB 自身當唯一真相）。
--
-- timeframe：兩個家族的 fact row 都自己帶，不需要 JOIN factor_registry（domain 知識寫死，
-- 見上面 factor_registry 的註解）——不再用相鄰 ts 統計推算，因為樣本少的
-- factor（例如目前只有 2 筆的 us_short_interest）統計出來的間隔不可靠，
-- 也會隨新資料進來一直變動，不是穩定的描述。
-- ============================================================
CREATE OR REPLACE VIEW data_inventory AS
SELECT
    'ohlcv' AS table_name,
    symbol,
    data_source,
    timeframe,
    instrument_type,
    calendar_id,
    session_mode,
    NULL::TEXT AS factor_name,
    count(*) AS rows,
    min(ts) AS start_ts,
    max(ts) AS end_ts
FROM ohlcv
GROUP BY symbol, data_source, timeframe, instrument_type, calendar_id, session_mode

UNION ALL

SELECT
    'external_factors' AS table_name,
    ef.symbol,
    ef.data_source,
    ef.timeframe,
    ef.instrument_type,
    NULL::TEXT AS calendar_id,
    NULL::TEXT AS session_mode,
    ef.factor_name,
    count(*) AS rows,
    min(ef.ts) AS start_ts,
    max(ef.ts) AS end_ts
FROM external_factors ef
GROUP BY ef.symbol, ef.data_source, ef.timeframe, ef.instrument_type, ef.factor_name

ORDER BY table_name, symbol, factor_name;

DO $$
BEGIN
    IF to_regclass('public.backtest_runs') IS NULL
       OR to_regclass('public.execution_runtime_state') IS NULL
       OR to_regclass('public.broker_orders') IS NULL
       OR to_regclass('public.position_events') IS NULL
       OR to_regclass('public.runtime_events') IS NULL
       OR NOT EXISTS (
           SELECT 1
           FROM information_schema.columns
           WHERE table_schema = 'public'
             AND table_name = 'backtest_runs'
             AND column_name = 'execution_identity'
       ) THEN
        RAISE EXCEPTION 'bootstrap did not produce the complete Librae schema revision 3';
    END IF;
END
$$;

COMMIT;
