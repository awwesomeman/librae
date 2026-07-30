-- One fake row per table, purely to eyeball the schema/format locally.
-- Safe to re-run (ON CONFLICT DO NOTHING everywhere there's a unique key).
-- Clean up with: psql "$TIMESCALE_DSN" -c "DELETE FROM backtest_runs WHERE run_id = 'seed_test_run';"
-- (equity_curve/trade_events/strategy_performance cascade-delete with it;
--  ohlcv/ohlcv_coverage_ranges/signal_events don't FK to run_id, delete separately if wanted)

INSERT INTO backtest_runs
    (run_id, strategy, symbols, timeframe, data_source, started_at, ended_at, run_at,
     mode, poll_seconds, params, execution_policy, risk_policy, config_hash)
VALUES
    ('seed_test_run', 'seed_test', '["BTCUSDT"]'::jsonb, 'H1', 'binance_spot',
     NOW() - INTERVAL '10 days', NOW(), NOW(),
     'backtest', NULL, '{}'::jsonb,
     '{"default_fill_price": "open", "max_bar_volume_participation_rate": 0.1, "warmup_periods": 720}'::jsonb,
     '{"max_position_weight": 0.3}'::jsonb,
     md5('seed_test_run'))
ON CONFLICT (run_id) DO NOTHING;

INSERT INTO equity_curve
    (ts, run_id, account_id, currency, equity, drawdown, period_return, strategy)
VALUES
    (NOW(), 'seed_test_run', 'default', 'USDT', 100500, -0.01, 0.005, 'seed_test')
ON CONFLICT (run_id, account_id, ts) DO NOTHING;

INSERT INTO trade_events
    (event_id, run_id, account_id, currency, strategy, mode, timeframe, ts,
     symbol, side, event_type,
     fill_quantity, price, entry_price, remaining_quantity, notional,
     commission, slippage, tax, pnl, net_return, entry_at, periods_held, reason)
VALUES
    ('seed_evt_1', 'seed_test_run', 'default', 'USDT',
     'seed_test', 'backtest', 'H1', NOW(),
     'BTCUSDT', 'long', 'close',
     0.1, 65000, 64000, 0, 6500,
     1.2, 0.5, 0, 95, 0.0148, NOW() - INTERVAL '2 hours', 2, 'exit_signal')
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
    (NOW(), 'BTCUSDT', 'H1', 'binance_spot', 64900, 65200, 64800, 65000, 123.45)
ON CONFLICT (ts, symbol, timeframe, data_source, instrument_type) DO NOTHING;

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
ON CONFLICT (ts, run_id, strategy, symbol, mode, timeframe, signal_type) DO NOTHING;
