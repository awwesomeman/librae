-- One fake row per table, purely to eyeball the schema/format locally.
-- Safe to re-run (ON CONFLICT DO NOTHING everywhere there's a unique key).
-- Clean up with: psql "$TIMESCALE_DSN" -c "DELETE FROM backtest_runs WHERE run_id = 'seed_test_run';"
-- (equity_curve/trade_events/strategy_performance cascade-delete with it;
--  ohlcv/ohlcv_coverage_ranges/signal_events don't FK to run_id, delete separately if wanted)

INSERT INTO backtest_runs
    (run_id, strategy, symbols, timeframe, data_source, started_at, ended_at, run_at,
     mode, poll_seconds, params, execution_policy, risk_policy, perf_params, config_hash)
VALUES
    ('seed_test_run', 'seed_test', '["BTCUSDT"]'::jsonb, 'H1', 'binance_spot',
     NOW() - INTERVAL '10 days', NOW(), NOW(),
     'backtest', NULL, '{"warmup_periods": 720}'::jsonb,
     '{"default_fill_price": "open", "max_volume_participation_rate": 0.1}'::jsonb,
     '{"max_position_weight": 0.3}'::jsonb,
     '{"annualize": true, "annual_periods": 365}'::jsonb, md5('seed_test_run'))
ON CONFLICT (run_id) DO NOTHING;

INSERT INTO equity_curve
    (ts, run_id, equity, benchmark_equity, drawdown, period_return, benchmark_period_return, strategy)
VALUES
    (NOW(), 'seed_test_run', 100500, 100000, -0.01, 0.005, 0.001, 'seed_test')
ON CONFLICT (run_id, ts) DO NOTHING;

INSERT INTO trade_events
    (event_id, run_id, strategy, mode, timeframe, ts, symbol, side, event_type,
     fill_quantity, price, entry_price, remaining_quantity, notional,
     commission, slippage, tax, pnl, net_return, entry_at, periods_held, reason)
VALUES
    ('seed_evt_1', 'seed_test_run', 'seed_test', 'backtest', 'H1', NOW(),
     'BTCUSDT', 'long', 'close',
     0.1, 65000, 64000, 0, 6500,
     1.2, 0.5, 0, 95, 0.0148, NOW() - INTERVAL '2 hours', 2, 'exit_signal')
ON CONFLICT (event_id, ts) DO NOTHING;

INSERT INTO strategy_performance
    (run_id, total_return, annual_return, sharpe, sortino, calmar, max_drawdown,
     win_rate, profit_factor, trades, avg_trade_return, exposure_ratio,
     benchmark_return, total_commission, total_slippage, total_tax)
VALUES
    ('seed_test_run', 0.05, 0.60, 1.2, 1.5, 2.0, -0.03,
     0.55, 1.8, 10, 0.005, 0.4,
     0.03, 12, 5, 0)
ON CONFLICT (run_id) DO NOTHING;

INSERT INTO ohlcv (ts, symbol, timeframe, data_source, open, high, low, close, volume)
VALUES
    (NOW(), 'BTCUSDT', 'H1', 'binance_spot', 64900, 65200, 64800, 65000, 123.45)
ON CONFLICT (ts, symbol, timeframe, data_source) DO NOTHING;

INSERT INTO ohlcv_coverage_ranges (symbol, timeframe, data_source, range_started_at, range_ended_at)
SELECT 'BTCUSDT', 'H1', 'binance_spot', NOW() - INTERVAL '10 days', NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM ohlcv_coverage_ranges
    WHERE symbol = 'BTCUSDT' AND timeframe = 'H1' AND data_source = 'binance_spot'
);

INSERT INTO signal_events
    (ts, run_id, strategy, symbol, mode, timeframe, signal_value, price, signal_type)
VALUES
    (NOW(), 'seed_test_run', 'seed_test', 'BTCUSDT', 'backtest', 'H1', 1.0, 65000, 'entry')
ON CONFLICT (ts, strategy, symbol, mode, timeframe, signal_type) DO NOTHING;
