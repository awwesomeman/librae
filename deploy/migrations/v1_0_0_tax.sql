-- Migration: v1_0_0_tax
-- Applies schema changes for tax tracking and data integrity.
-- Run manually on existing databases:
--   psql -U quant -d quant -f deploy/migrations/v1_0_0_tax.sql

-- 1. Add tax column to trade_blotter
ALTER TABLE trade_blotter ADD COLUMN IF NOT EXISTS tax DOUBLE PRECISION DEFAULT 0;

-- 2. Add total_tax column to strategy_performance
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS total_tax DOUBLE PRECISION DEFAULT 0;

-- 3. Fix equity_curve unique index column order (run_id first for query efficiency)
DROP INDEX IF EXISTS idx_equity_curve_unique;
CREATE UNIQUE INDEX IF NOT EXISTS idx_equity_curve_unique ON equity_curve(run_id, ts);

-- 4. Add FK constraint to strategy_signals (skip if already exists)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'strategy_signals_run_id_fkey'
          AND table_name = 'strategy_signals'
    ) THEN
        ALTER TABLE strategy_signals
            ADD CONSTRAINT strategy_signals_run_id_fkey
            FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id) ON DELETE CASCADE;
    END IF;
END $$;

-- 5. Make ohlcv.run_id NOT NULL + add FK constraint (skip if already exists)
-- NOTE: This will fail if there are existing NULL run_id rows in ohlcv.
-- Clean up first: DELETE FROM ohlcv WHERE run_id IS NULL;
ALTER TABLE ohlcv ALTER COLUMN run_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'ohlcv_run_id_fkey'
          AND table_name = 'ohlcv'
    ) THEN
        ALTER TABLE ohlcv
            ADD CONSTRAINT ohlcv_run_id_fkey
            FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id) ON DELETE CASCADE;
    END IF;
END $$;
