-- Dedicated seed for testing Trade P&L Distribution's histogram binning —
-- seed_fake_data.sql only has 5 closed trades (too few to show a real
-- distribution shape). This adds 200 closed trades with an approximately
-- normal P&L spread (sum of 4 uniforms, Irwin-Hall/CLT approximation),
-- under its own run_id so it never collides with the hand-crafted demo data.
--
-- Self-cleaning, same convention as seed_fake_data.sql: delete before
-- insert so re-running always reflects the current version of this file.
--
-- Cleanup:
--   psql "$TIMESCALE_DSN" -c "
--     DELETE FROM backtest_runs WHERE run_id = 'seed_pnl_dist_run';
--     DELETE FROM position_events WHERE run_id = 'seed_pnl_dist_run';
--   "

DELETE FROM position_events WHERE run_id = 'seed_pnl_dist_run';
DELETE FROM backtest_runs WHERE run_id = 'seed_pnl_dist_run';

INSERT INTO backtest_runs
    (run_id, strategy_name, symbols, timeframe, data_source, data_source_by_symbol,
     started_at, ended_at, run_at, mode, poll_seconds, params, execution_policy,
     risk_policy, config_hash)
VALUES
    ('seed_pnl_dist_run', 'seed_test', '["BTCUSDT", "ETHUSDT", "SOLUSDT"]'::jsonb, 'H1', 'binance_spot',
     '{"BTCUSDT":"binance_spot","ETHUSDT":"binance_spot","SOLUSDT":"binance_spot"}'::jsonb,
     NOW() - INTERVAL '200 hours', NOW(), NOW(),
     'backtest', NULL, '{}'::jsonb,
     '{"default_fill_price": "open", "max_bar_volume_participation_rate": 0.1, "warmup_periods": 720}'::jsonb,
     '{"max_position_weight": 0.3}'::jsonb,
     md5('seed_pnl_dist_run'))
ON CONFLICT (run_id) DO NOTHING;

-- pnl_value is computed in this CTE (not a LATERAL joined to generate_series
-- with no correlated reference) so Postgres evaluates random() once per row
-- instead of hoisting the uncorrelated subquery and reusing one value for all.
WITH pnl_gen AS (
    -- Irwin-Hall(4) rescaled to mean 0, spread roughly -800..800
    SELECT i, (random() + random() + random() + random() - 2) * 400 AS pnl_value
    FROM generate_series(1, 200) AS i
)
INSERT INTO position_events
    (event_id, run_id, account_id, currency, strategy_name, mode, timeframe, ts,
     symbol, side, event_type,
     fill_quantity, price, entry_price, remaining_quantity, notional,
     commission, slippage, tax, realized_pnl, net_return, entry_at, periods_held, reason)
SELECT
    'seed_pnl_dist_evt_' || i,
    'seed_pnl_dist_run', 'default', 'USDT', 'seed_test', 'backtest', 'H1',
    NOW() - ((200 - i) || ' hours')::interval,
    (ARRAY['BTCUSDT', 'ETHUSDT', 'SOLUSDT'])[1 + (i % 3)],
    CASE WHEN pnl_value >= 0 THEN 'long' ELSE 'short' END,
    'close',
    0.1, 100, 100, 0, 10000,
    1.0, 0.5, 0,
    ROUND(pnl_value::numeric, 2)::float8,
    -- net_return is already percent-scale (net_pnl / entry_notional * 100,
    -- see executor.py) — NOT a 0-1 fraction, matches Trade Events' "percent" unit.
    ROUND((pnl_value / 10000 * 100)::numeric, 2)::float8,
    NOW() - ((200 - i + 5) || ' hours')::interval,
    5, 'seed'
FROM pnl_gen
ON CONFLICT (event_id, ts) DO NOTHING;
