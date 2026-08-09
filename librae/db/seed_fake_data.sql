-- Fuller demo dataset spanning 14 days — enough history to see Price Trend,
-- Equity Curve, and Position Weight actually move, not just single points.
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
--     DELETE FROM runtime_events WHERE run_id = 'seed_test_run';
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
     NOW() - INTERVAL '14 days', NOW(), NOW(),
     'backtest', NULL, '{}'::jsonb,
     '{"default_fill_price": "open", "max_bar_volume_participation_rate": 0.1, "warmup_periods": 720}'::jsonb,
     '{"max_position_weight": 0.3}'::jsonb,
     md5('seed_test_run'))
ON CONFLICT (run_id) DO NOTHING;

-- Hourly OHLCV over the full 14-day window — smooth trend + small noise, one
-- per symbol. Only feeds Price Trend/Entry-Exit Signals; Position Weight
-- reads trade_events.price directly, so this doesn't need to line up with
-- the trade narrative below.
INSERT INTO ohlcv (ts, symbol, timeframe, data_source, open, high, low, close, volume)
SELECT
    ts,
    sym.symbol,
    'H1',
    'binance_spot',
    base * (1 + 0.08 * sin(i / 18.0) + (random() - 0.5) * 0.01) AS open,
    base * (1 + 0.08 * sin(i / 18.0) + (random() - 0.5) * 0.01) * 1.004 AS high,
    base * (1 + 0.08 * sin(i / 18.0) + (random() - 0.5) * 0.01) * 0.996 AS low,
    base * (1 + 0.08 * sin((i + 1) / 18.0) + (random() - 0.5) * 0.01) AS close,
    base_vol * (0.7 + random() * 0.6) AS volume
FROM generate_series(0, 14 * 24 - 1) AS i,
     LATERAL (SELECT NOW() - INTERVAL '14 days' + (i || ' hours')::interval AS ts) t,
     (VALUES ('BTCUSDT', 60000.0, 150.0),
             ('ETHUSDT', 3000.0, 500.0),
             ('SOLUSDT', 130.0, 900.0)) AS sym(symbol, base, base_vol)
ON CONFLICT (ts, symbol, timeframe, data_source, instrument_type) DO NOTHING;

INSERT INTO ohlcv_coverage_ranges (symbol, timeframe, data_source, range_started_at, range_ended_at)
SELECT s, 'H1', 'binance_spot', NOW() - INTERVAL '14 days', NOW()
FROM unnest(ARRAY['BTCUSDT', 'ETHUSDT', 'SOLUSDT']) AS s
WHERE NOT EXISTS (
    SELECT 1 FROM ohlcv_coverage_ranges
    WHERE symbol = s AND timeframe = 'H1' AND data_source = 'binance_spot'
);

-- Hourly equity_curve over the same window — mild uptrend with one
-- drawdown dip, so Equity Curve/Drawdown/Sharpe-type panels have something
-- to show. Position Weight resolves each trade against the nearest
-- at-or-before row here via a LATERAL join.
INSERT INTO equity_curve
    (ts, run_id, account_id, currency, equity, drawdown, period_return,
     gross_exposure, net_exposure, concentration, turnover, strategy)
SELECT
    NOW() - INTERVAL '14 days' + (i || ' hours')::interval,
    'seed_test_run', 'default', 'USDT',
    equity,
    LEAST(0, (equity - peak) / peak) AS drawdown,
    (equity - lag_equity) / lag_equity AS period_return,
    0.15 + random() * 0.15,
    0.05 + random() * 0.1,
    0.1 + random() * 0.15,
    random() * 0.05,
    'seed_test'
FROM (
    SELECT i,
        equity,
        MAX(equity) OVER (ORDER BY i) AS peak,
        LAG(equity) OVER (ORDER BY i) AS lag_equity
    FROM (
        SELECT i,
            100000 * (1 + 0.06 * (i / (14.0 * 24))
                - CASE WHEN i BETWEEN 150 AND 200 THEN 0.03 * sin((i - 150) / 50.0 * pi()) ELSE 0 END
                + (random() - 0.5) * 0.002) AS equity
        FROM generate_series(0, 14 * 24 - 1) AS i
    ) base
) sized
WHERE lag_equity IS NOT NULL
ON CONFLICT (run_id, account_id, ts) DO NOTHING;

-- Trade narrative across the 14-day window — rotation (BTC -> ETH), a
-- partial reduce, a same-bar multi-leg arb pair (BTC/SOL sharing
-- group_id), and a final reopen still open at "now". Exercises every
-- lifecycle shape discussed: scale-in weighted-average entry, full close
-- resetting entry_at on reopen, partial reduce, and arb-pair correlation.
INSERT INTO trade_events
    (event_id, run_id, account_id, currency, strategy, mode, timeframe, ts,
     symbol, side, event_type,
     fill_quantity, price, entry_price, remaining_quantity, notional,
     commission, slippage, tax, pnl, net_return, entry_at, periods_held, reason,
     group_id, time_in_force)
VALUES
    -- Lifecycle 1: BTCUSDT open -> add -> close (day -13 to day -9)
    ('seed_evt_1', 'seed_test_run', 'default', 'USDT', 'seed_test', 'backtest', 'H1',
     NOW() - INTERVAL '13 days', 'BTCUSDT', 'long', 'open',
     0.1, 60000, 60000, 0.1, 6000, 0.6, 0.2, 0, NULL, NULL,
     NOW() - INTERVAL '13 days', 0, 'entry_signal', NULL, 'day'),
    ('seed_evt_2', 'seed_test_run', 'default', 'USDT', 'seed_test', 'backtest', 'H1',
     NOW() - INTERVAL '11 days', 'BTCUSDT', 'long', 'add',
     0.05, 61000, 60333.33, 0.15, 3050, 0.4, 0.1, 0, NULL, NULL,
     NOW() - INTERVAL '13 days', 48, 'scale_in', NULL, 'day'),
    ('seed_evt_3', 'seed_test_run', 'default', 'USDT', 'seed_test', 'backtest', 'H1',
     NOW() - INTERVAL '9 days' - INTERVAL '1 hour', 'BTCUSDT', 'long', 'close',
     0.15, 62000, 60333.33, 0, 9300, 1.2, 0.5, 0, 250.00, 0.0276,
     NOW() - INTERVAL '13 days', 94, 'take_profit', NULL, 'day'),
    -- Rotation: close BTC, open ETH the same day (day -9)
    ('seed_evt_4', 'seed_test_run', 'default', 'USDT', 'seed_test', 'backtest', 'H1',
     NOW() - INTERVAL '9 days', 'ETHUSDT', 'long', 'open',
     1.5, 3100, 3100, 1.5, 4650, 0.5, 0.2, 0, NULL, NULL,
     NOW() - INTERVAL '9 days', 0, 'rotation_entry', NULL, 'day'),
    -- Partial reduce (day -7)
    ('seed_evt_5', 'seed_test_run', 'default', 'USDT', 'seed_test', 'backtest', 'H1',
     NOW() - INTERVAL '7 days', 'ETHUSDT', 'long', 'reduce',
     0.5, 3250, 3100, 1.0, 1625, 0.3, 0.1, 0, 75.00, 0.0484,
     NOW() - INTERVAL '9 days', 48, 'trim_signal', NULL, 'day'),
    -- Arb pair opened on the same bar, same group_id, different symbols (day -6)
    ('seed_evt_6', 'seed_test_run', 'default', 'USDT', 'seed_test', 'backtest', 'H1',
     NOW() - INTERVAL '6 days', 'BTCUSDT', 'long', 'open',
     0.08, 63000, 63000, 0.08, 5040, 0.5, 0.15, 0, NULL, NULL,
     NOW() - INTERVAL '6 days', 0, 'basis_arb_entry', 'funding_arb_1', 'day'),
    ('seed_evt_7', 'seed_test_run', 'default', 'USDT', 'seed_test', 'backtest', 'H1',
     NOW() - INTERVAL '6 days', 'SOLUSDT', 'short', 'open',
     15, 135, 135, 15, 2025, 0.3, 0.08, 0, NULL, NULL,
     NOW() - INTERVAL '6 days', 0, 'basis_arb_entry', 'funding_arb_1', 'day'),
    -- ETH lifecycle closes out (day -4)
    ('seed_evt_8', 'seed_test_run', 'default', 'USDT', 'seed_test', 'backtest', 'H1',
     NOW() - INTERVAL '4 days', 'ETHUSDT', 'long', 'close',
     1.0, 3400, 3100, 0, 3400, 0.6, 0.2, 0, 300.00, 0.0968,
     NOW() - INTERVAL '9 days', 120, 'exit_signal', NULL, 'day'),
    -- Arb unwind, both legs closed on the same bar (day -2)
    ('seed_evt_9', 'seed_test_run', 'default', 'USDT', 'seed_test', 'backtest', 'H1',
     NOW() - INTERVAL '2 days', 'BTCUSDT', 'long', 'close',
     0.08, 64000, 63000, 0, 5120, 0.5, 0.15, 0, 80.00, 0.0159,
     NOW() - INTERVAL '6 days', 96, 'basis_arb_exit', 'funding_arb_1', 'day'),
    ('seed_evt_10', 'seed_test_run', 'default', 'USDT', 'seed_test', 'backtest', 'H1',
     NOW() - INTERVAL '2 days', 'SOLUSDT', 'short', 'close',
     15, 132, 135, 0, 1980, 0.3, 0.08, 0, 45.00, 0.0222,
     NOW() - INTERVAL '6 days', 96, 'basis_arb_exit', 'funding_arb_1', 'day'),
    -- Fresh BTC lifecycle, still open at "now" (day -1)
    ('seed_evt_11', 'seed_test_run', 'default', 'USDT', 'seed_test', 'backtest', 'H1',
     NOW() - INTERVAL '1 day', 'BTCUSDT', 'long', 'open',
     0.06, 65000, 65000, 0.06, 3900, 0.4, 0.12, 0, NULL, NULL,
     NOW() - INTERVAL '1 day', 0, 'entry_signal', NULL, 'day')
ON CONFLICT (event_id, ts) DO NOTHING;

INSERT INTO strategy_performance
    (run_id, account_id, currency, initial_cash, final_equity, net_pnl,
     total_return, mean_period_return, period_volatility,
     period_downside_deviation, period_sharpe, period_sortino,
     positive_period_rate, max_drawdown,
     win_rate, profit_factor, trades, avg_trade_return, exposure_ratio,
     total_commission, total_slippage, total_tax)
VALUES
    ('seed_test_run', 'default', 'USDT', 100000, 106500, 6500,
     0.065, 0.0004, 0.008, 0.005, 0.06, 0.09, 0.56, -0.03,
     1.0, 0, 5, 0.0342, 0.55,
     4.4, 1.4, 0)
ON CONFLICT (run_id, account_id) DO NOTHING;

INSERT INTO signal_events
    (ts, run_id, strategy, symbol, mode, timeframe, signal_value, price, signal_type)
VALUES
    (NOW() - INTERVAL '13 days', 'seed_test_run', 'seed_test', 'BTCUSDT', 'backtest', 'H1', 1.0, 60000, 'entry'),
    (NOW() - INTERVAL '9 days' - INTERVAL '1 hour', 'seed_test_run', 'seed_test', 'BTCUSDT', 'backtest', 'H1', -1.0, 62000, 'exit'),
    (NOW() - INTERVAL '9 days', 'seed_test_run', 'seed_test', 'ETHUSDT', 'backtest', 'H1', 1.0, 3100, 'entry'),
    (NOW() - INTERVAL '4 days', 'seed_test_run', 'seed_test', 'ETHUSDT', 'backtest', 'H1', -1.0, 3400, 'exit'),
    (NOW() - INTERVAL '1 day', 'seed_test_run', 'seed_test', 'BTCUSDT', 'backtest', 'H1', 1.0, 65000, 'entry')
ON CONFLICT (ts, run_id, strategy, symbol, mode, timeframe, signal_type) DO NOTHING;

-- Cascades with backtest_runs (real FK), unlike trade_events/signal_events above.
INSERT INTO runtime_events (ts, run_id, event_type, symbol, detail)
VALUES
    (NOW() - INTERVAL '13 days' + INTERVAL '5 minutes', 'seed_test_run', 'state_recovered', NULL,
     '{"last_cycle_ts": null, "active_orders": 0, "halted": false}'::jsonb),
    (NOW() - INTERVAL '10 days', 'seed_test_run', 'decision_skipped', 'ETHUSDT',
     '{"reason": "insufficient_cash"}'::jsonb),
    (NOW() - INTERVAL '8 days', 'seed_test_run', 'state_recovered', NULL,
     '{"last_cycle_ts": "restart", "active_orders": 1, "halted": false}'::jsonb),
    (NOW() - INTERVAL '6 days' - INTERVAL '30 minutes', 'seed_test_run', 'decision_skipped', 'SOLUSDT',
     '{"reason": "opposite_side"}'::jsonb),
    (NOW() - INTERVAL '3 days', 'seed_test_run', 'decision_skipped', 'BTCUSDT',
     '{"reason": "notional_capped"}'::jsonb)
ON CONFLICT (run_id, ts, event_type, COALESCE(symbol, '')) DO NOTHING;
