-- Bring a database created before subscription identity up to the current
-- column shape. The Python runner owns one transaction plus the schema
-- advisory lock.
--
-- Additive only, and every statement is idempotent: a database bootstrapped
-- from a newer file already has some of these columns, and reaches this
-- migration only for the ones it lacks.
--
-- The ohlcv columns land nullable on purpose. That table is a hypertable with
-- millions of rows across hundreds of chunks, so a nullable add is a metadata
-- change while a NOT NULL add would rewrite every chunk inside this
-- transaction. A one-off backfill fills them in committed batches afterwards
-- (named by the warning `librae db preflight` prints), and a later revision
-- enforces NOT NULL once no nulls remain.
-- Expand here, backfill, contract later — one release cannot do all three
-- without holding a rewrite open across an operator's backfill.
--
-- Deliberately not touched: financing_cash_flows. An adopted database can
-- carry entry_at nulls from before that column was required, and constraint
-- names from before the funding -> financing rename. Neither blocks the
-- engine — no schema guard reads that table and the writer supplies the
-- column — and forcing NOT NULL would mean inventing an entry time for rows
-- that never recorded one. It is a data gap to close deliberately, not a
-- side effect of adopting a schema.

-- ============================================================
-- backtest_runs — run routing and subscription identity
-- ============================================================
ALTER TABLE backtest_runs
    ADD COLUMN IF NOT EXISTS data_source_by_symbol JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS primary_subscriptions JSONB,
    ADD COLUMN IF NOT EXISTS session_mode TEXT NOT NULL DEFAULT 'extended';

-- Runs recorded before subscriptions were persisted have none to record. The
-- empty array is what chk_primary_subscriptions_array admits for that case;
-- inventing a subscription per symbol would assert routing nobody captured.
UPDATE backtest_runs SET primary_subscriptions = '[]'::jsonb
WHERE primary_subscriptions IS NULL;

ALTER TABLE backtest_runs ALTER COLUMN primary_subscriptions SET NOT NULL;

DO $$ BEGIN
    ALTER TABLE backtest_runs ADD CONSTRAINT chk_run_data_sources_object
        CHECK (jsonb_typeof(data_source_by_symbol) = 'object');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE backtest_runs ADD CONSTRAINT chk_primary_subscriptions_array
        CHECK (
            jsonb_typeof(primary_subscriptions) = 'array'
            AND (
                jsonb_array_length(primary_subscriptions) = 0
                OR jsonb_array_length(primary_subscriptions) = jsonb_array_length(symbols)
            )
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE backtest_runs ADD CONSTRAINT chk_session_mode
        CHECK (session_mode IN ('regular', 'extended'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ============================================================
-- broker_orders — cancel intent survives a restart
-- ============================================================
ALTER TABLE broker_orders
    ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE;

-- ============================================================
-- symbols — executable quantity metadata
-- ============================================================
ALTER TABLE symbols
    ADD COLUMN IF NOT EXISTS quantity_step DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS min_quantity DOUBLE PRECISION;

DO $$ BEGIN
    ALTER TABLE symbols ADD CONSTRAINT chk_symbols_quantity_step
        CHECK (quantity_step IS NULL OR quantity_step > 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE symbols ADD CONSTRAINT chk_symbols_min_quantity
        CHECK (min_quantity IS NULL OR min_quantity > 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ============================================================
-- ohlcv — session identity and point-in-time availability
-- ============================================================
-- session_mode defaults every adopted row to 'extended'. That is the engine's
-- own default and the only answer available: nothing in an adopted row records
-- which session it came from. Reads filter on it, so a bar that was in fact
-- regular-session becomes readable only by an extended subscription. Rows
-- whose provenance is known can be corrected afterwards; there is nothing to
-- derive it from here.
ALTER TABLE ohlcv
    ADD COLUMN IF NOT EXISTS calendar_id TEXT,
    ADD COLUMN IF NOT EXISTS session_mode TEXT NOT NULL DEFAULT 'extended',
    ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ;

DO $$ BEGIN
    ALTER TABLE ohlcv ADD CONSTRAINT chk_ohlcv_session_mode
        CHECK (session_mode IN ('regular', 'extended'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- The writer's ON CONFLICT names the session-aware key, so the index has to
-- carry it before the engine can write at all. Existing rows cannot collide
-- under the wider key: the narrower index this replaces already made them
-- unique, and a null calendar_id is distinct from every other value.
--
-- This rebuild, not the column adds, is what sets the maintenance window. It
-- is full-table work under an ACCESS EXCLUSIVE lock, the same order of
-- magnitude the nullable adds above exist to avoid — unavoidable here,
-- because the writer cannot use the old key at all.
-- Only where it is actually wrong. A database bootstrapped from a current
-- timescale_init.sql already has this exact index, and rebuilding it would
-- charge every such deployment a full-table ACCESS EXCLUSIVE lock for no
-- change at all.
DO $$
DECLARE
    existing TEXT;
BEGIN
    SELECT indexdef INTO existing FROM pg_indexes
     WHERE schemaname = 'public' AND indexname = 'idx_ohlcv_unique';
    IF existing IS NULL OR existing NOT LIKE '%calendar_id%' THEN
        DROP INDEX IF EXISTS idx_ohlcv_unique;
        CREATE UNIQUE INDEX idx_ohlcv_unique
            ON ohlcv (
                ts, symbol, timeframe, calendar_id, session_mode, data_source, instrument_type
            );
    END IF;
END
$$;

ALTER TABLE ohlcv_coverage_ranges
    ADD COLUMN IF NOT EXISTS calendar_id TEXT,
    ADD COLUMN IF NOT EXISTS session_mode TEXT NOT NULL DEFAULT 'extended';

DO $$ BEGIN
    ALTER TABLE ohlcv_coverage_ranges ADD CONSTRAINT chk_ohlcv_coverage_session_mode
        CHECK (session_mode IN ('regular', 'extended'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_ohlcv_coverage_ranges_lookup
    ON ohlcv_coverage_ranges(
        symbol, timeframe, calendar_id, session_mode, data_source, instrument_type,
        range_started_at
    );

-- ============================================================
-- data_inventory — the view reports the new identity columns
-- ============================================================
-- Dropped rather than replaced: CREATE OR REPLACE VIEW can only append
-- columns, and these belong in the middle of the select list.
--
-- Dropping a view drops every ACL on it, including grants this migration has
-- no way to know about — a BI account, a read replica's reader. Restoring
-- only the two managed roles would silently revoke the rest, so the existing
-- ACL is captured first and replayed after.
CREATE TEMP TABLE data_inventory_acl ON COMMIT DROP AS
SELECT relacl AS acl, relowner
  FROM pg_class
 WHERE oid = to_regclass('public.data_inventory') AND relacl IS NOT NULL;

DROP VIEW IF EXISTS data_inventory;
CREATE VIEW data_inventory AS
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

-- Replay every table-level privilege the view had, grant option included; the
-- owner is skipped because it holds everything implicitly. Column-level grants
-- (attacl) are also dropped with the view and are deliberately not replayed.
-- Then ensure the two roles librae manages, which covers a view that had no
-- ACL. Those two are guarded because a restored database may not have the
-- roles at all.
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT a.grantee, a.privilege_type, a.is_grantable
          FROM data_inventory_acl s, aclexplode(s.acl) a
         WHERE a.grantee <> s.relowner
    LOOP
        EXECUTE format(
            'GRANT %s ON data_inventory TO %s%s',
            r.privilege_type,
            CASE WHEN r.grantee = 0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(r.grantee)) END,
            CASE WHEN r.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END
        );
    END LOOP;

    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'quant_app') THEN
        EXECUTE 'GRANT SELECT ON data_inventory TO quant_app';
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_reader') THEN
        EXECUTE 'GRANT SELECT ON data_inventory TO grafana_reader';
    END IF;
END
$$;

UPDATE librae_schema_revision
SET revision = 3, updated_at = NOW()
WHERE singleton = TRUE AND revision = 2;
