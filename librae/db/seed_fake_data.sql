-- Fake rows per table, purely to eyeball the schema/format locally.
--
-- Self-cleaning: deletes its own seed_test_run/BTCUSDT+ETHUSDT+SOLUSDT rows
-- before inserting, so it's safe to re-run any number of times and always
-- reflects the current version of this file. ON CONFLICT DO NOTHING alone is
-- NOT enough for that — most rows below are keyed off NOW(), so a second run
-- produces different timestamps and inserts *new* rows instead of no-oping
-- on the old ones, silently piling up stale duplicates (this bit us once —
-- trade_events isn't FK-cascaded from backtest_runs, see the note below).
--
-- trade_events/signal_events deliberately have no FK to backtest_runs
-- (`ff9baf4c "unify field naming"` — standalone queryability without
-- joining backtest_runs), so deleting backtest_runs does NOT cascade to
-- them; they're cleaned up explicitly below along with ohlcv/
-- ohlcv_coverage_ranges, which never had a run_id column to cascade from.
--
-- Manual cleanup (equity_curve/strategy_performance cascade with backtest_runs):
--   psql "$TIMESCALE_DSN" -c "
--     DELETE FROM backtest_runs WHERE run_id = 'seed_test_run';
--     DELETE FROM trade_events WHERE run_id = 'seed_test_run';
--     DELETE FROM signal_events WHERE run_id = 'seed_test_run';
--     DELETE FROM ohlcv WHERE symbol IN ('BTCUSDT', 'ETHUSDT', 'SOLUSDT');
--     DELETE FROM ohlcv_coverage_ranges WHERE symbol IN ('BTCUSDT', 'ETHUSDT', 'SOLUSDT');
--   "

DELETE FROM trade_events WHERE run_id = 'seed_test_run';
DELETE FROM signal_events WHERE run_id = 'seed_test_run';
DELETE FROM ohlcv WHERE symbol IN ('BTCUSDT', 'ETHUSDT', 'SOLUSDT');
DELETE FROM ohlcv_coverage_ranges WHERE symbol IN ('BTCUSDT', 'ETHUSDT', 'SOLUSDT');
-- Cascades to equity_curve/strategy_performance (real FK ON DELETE CASCADE).
DELETE FROM backtest_runs WHERE run_id = 'seed_test_run';

INSERT INTO backtest_runs
    (run_id, strategy, symbols, timeframe, data_source, started_at, ended_at, run_at,
     mode, poll_seconds, params, execution_policy, risk_policy, config_hash)
VALUES
    ('seed_test_run', 'seed_test', '["BTCUSDT", "ETHUSDT", "SOLUSDT"]'::jsonb, 'H1', 'binance_spot',
     NOW() - INTERVAL '10 days', NOW(), NOW(),
     'backtest', NULL, '{}'::jsonb,
     '{"default_fill_price": "open", "max_bar_volume_participation_rate": 0.1, "warmup_periods": 720}'::jsonb,
     '{"max_position_weight": 0.3}'::jsonb,
     md5('seed_test_run'))
ON CONFLICT (run_id) DO NOTHING;

INSERT INTO equity_curve
    (ts, run_id, account_id, currency, equity, drawdown, period_return,
     gross_exposure, net_exposure, concentration, turnover, strategy)
VALUES
    (NOW(), 'seed_test_run', 'default', 'USDT', 100500, -0.01, 0.005,
     0.35, 0.35, 0.35, 0.05, 'seed_test')
ON CONFLICT (run_id, account_id, ts) DO NOTHING;

-- Three scenarios below, all sharing the entry_at/Position convention
-- (entry_at is fixed at a lifecycle's first open, unchanged by later adds,
-- and reset only when the position fully closes and reopens from flat):
--
-- 1. seed_evt_1..3: BTCUSDT open -> add -> close, one lifecycle. entry_at is
--    the same (NOW() - 10h) on all three rows despite the weighted-average
--    entry_price changing on the add. This is what "Position" filters on.
-- 2. seed_evt_4: BTCUSDT open again after the above fully closed — a new
--    lifecycle gets a new entry_at, distinct from scenario 1's. Still open
--    (no close row), so it also shows up in the Open Positions panel.
-- 3. seed_evt_5/6: a funding-arb pair (ETHUSDT long + SOLUSDT short) opened
--    on the same bar with the same group_id — same Time/entry_at, but
--    different Position because Position includes symbol. This is the case
--    where filtering Entry Time alone would mix two unrelated legs together.
INSERT INTO trade_events
    (event_id, run_id, account_id, currency, strategy, mode, timeframe, ts,
     symbol, side, event_type,
     fill_quantity, price, entry_price, remaining_quantity, notional,
     commission, slippage, tax, pnl, net_return, entry_at, periods_held, reason,
     group_id, time_in_force)
VALUES
    ('seed_evt_1', 'seed_test_run', 'default', 'USDT',
     'seed_test', 'backtest', 'H1', NOW() - INTERVAL '10 hours',
     'BTCUSDT', 'long', 'open',
     0.1, 64000, 64000, 0.1, 6400,
     0.6, 0.2, 0, NULL, NULL, NOW() - INTERVAL '10 hours', 0, 'entry_signal',
     NULL, 'day'),
    ('seed_evt_2', 'seed_test_run', 'default', 'USDT',
     'seed_test', 'backtest', 'H1', NOW() - INTERVAL '9 hours',
     'BTCUSDT', 'long', 'add',
     0.05, 64500, 64166.67, 0.15, 3225,
     0.4, 0.1, 0, NULL, NULL, NOW() - INTERVAL '10 hours', 1, 'scale_in',
     NULL, 'day'),
    ('seed_evt_3', 'seed_test_run', 'default', 'USDT',
     'seed_test', 'backtest', 'H1', NOW() - INTERVAL '8 hours',
     'BTCUSDT', 'long', 'close',
     0.15, 65000, 64166.67, 0, 9750,
     1.2, 0.5, 0, 125.0, 0.013, NOW() - INTERVAL '10 hours', 2, 'exit_signal',
     NULL, 'day'),
    -- Open position — demonstrates the Open Positions panel (Grafana filters
    -- to remaining_quantity > 0); a fresh lifecycle after the one above closed.
    ('seed_evt_4', 'seed_test_run', 'default', 'USDT',
     'seed_test', 'backtest', 'H1', NOW() - INTERVAL '6 hours',
     'BTCUSDT', 'long', 'open',
     0.05, 65500, 65500, 0.05, 3275,
     0.6, 0.2, 0, NULL, NULL, NOW() - INTERVAL '6 hours', 0, 'entry_signal',
     NULL, 'day'),
    ('seed_evt_5', 'seed_test_run', 'default', 'USDT',
     'seed_test', 'backtest', 'H1', NOW() - INTERVAL '5 hours',
     'ETHUSDT', 'long', 'open',
     1.0, 3200, 3200, 1.0, 3200,
     0.3, 0.1, 0, NULL, NULL, NOW() - INTERVAL '5 hours', 0, 'basis_arb_entry',
     'funding_arb_1', 'day'),
    ('seed_evt_6', 'seed_test_run', 'default', 'USDT',
     'seed_test', 'backtest', 'H1', NOW() - INTERVAL '5 hours',
     'SOLUSDT', 'short', 'open',
     10, 140, 140, 10, 1400,
     0.2, 0.05, 0, NULL, NULL, NOW() - INTERVAL '5 hours', 0, 'basis_arb_entry',
     'funding_arb_1', 'day')
ON CONFLICT (event_id, ts) DO NOTHING;

INSERT INTO strategy_performance
    (run_id, account_id, currency, initial_cash, final_equity, net_pnl,
     total_return, mean_period_return, period_volatility,
     period_downside_deviation, period_sharpe, period_sortino,
     positive_period_rate, max_drawdown,
     win_rate, profit_factor, trades, avg_trade_return, exposure_ratio,
     total_commission, total_slippage, total_tax)
VALUES
    ('seed_test_run', 'default', 'USDT', 100000, 105000, 5000,
     0.05, 0.0005, 0.01, 0.006, 0.05, 0.08, 0.55, -0.03,
     0.55, 1.8, 10, 0.005, 0.4,
     12, 5, 0)
ON CONFLICT (run_id, account_id) DO NOTHING;

INSERT INTO ohlcv (ts, symbol, timeframe, data_source, open, high, low, close, volume)
VALUES
    (NOW(), 'BTCUSDT', 'H1', 'binance_spot', 64900, 65200, 64800, 65000, 123.45),
    (NOW(), 'ETHUSDT', 'H1', 'binance_spot', 3180, 3220, 3170, 3200, 456.78),
    (NOW(), 'SOLUSDT', 'H1', 'binance_spot', 138, 142, 137, 140, 789.01)
ON CONFLICT (ts, symbol, timeframe, data_source, instrument_type) DO NOTHING;

INSERT INTO ohlcv_coverage_ranges (symbol, timeframe, data_source, range_started_at, range_ended_at)
SELECT s, 'H1', 'binance_spot', NOW() - INTERVAL '10 days', NOW()
FROM unnest(ARRAY['BTCUSDT', 'ETHUSDT', 'SOLUSDT']) AS s
WHERE NOT EXISTS (
    SELECT 1 FROM ohlcv_coverage_ranges
    WHERE symbol = s AND timeframe = 'H1' AND data_source = 'binance_spot'
);

INSERT INTO signal_events
    (ts, run_id, strategy, symbol, mode, timeframe, signal_value, price, signal_type)
VALUES
    (NOW(), 'seed_test_run', 'seed_test', 'BTCUSDT', 'backtest', 'H1', 1.0, 65000, 'entry')
ON CONFLICT (ts, run_id, strategy, symbol, mode, timeframe, signal_type) DO NOTHING;
