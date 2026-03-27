"""Tests for TrendPullBack BTC strategy core."""
from __future__ import annotations

import pandas as pd
import pytest

from librae.sample_data import generate_btc_h1_ohlcv
from strategies.trendpullback.btc import (
    Signal,
    TrendPullBackParams,
    add_daily_gate,
    add_h1_features,
    backtest,
    generate_signals,
    resample_ohlcv,
)


@pytest.fixture
def sample_data():
    df = generate_btc_h1_ohlcv(n_bars=2160)
    df = df.set_index("timestamp")
    m1 = df.copy()
    h1 = add_h1_features(df)
    d1 = add_daily_gate(resample_ohlcv(df, "1D"))
    return m1, h1, d1


@pytest.fixture
def params():
    return TrendPullBackParams()


class TestFeatureEngineering:
    def test_h1_features_columns(self, sample_data):
        _, h1, _ = sample_data
        assert "ema20" in h1.columns
        assert "atr14" in h1.columns
        assert "vol_sma20" in h1.columns

    def test_daily_gate_columns(self, sample_data):
        _, _, d1 = sample_data
        assert "ema20_prev" in d1.columns

    def test_resample_preserves_ohlcv(self, sample_data):
        m1, _, _ = sample_data
        daily = resample_ohlcv(m1, "1D")
        assert set(daily.columns) >= {"open", "high", "low", "close", "volume"}


class TestSignalGeneration:
    def test_generates_signals(self, sample_data, params):
        m1, h1, d1 = sample_data
        signals = generate_signals(m1, h1, d1, params)
        assert isinstance(signals, list)

    def test_signal_structure(self, sample_data, params):
        m1, h1, d1 = sample_data
        signals = generate_signals(m1, h1, d1, params)
        if signals:
            sig = signals[0]
            assert isinstance(sig, Signal)
            assert sig.side == "buy"
            assert sig.entry_price > 0
            assert sig.stop_price < sig.entry_price
            assert sig.target_price > sig.entry_price
            assert 0 <= sig.strength <= 1


class TestBacktest:
    def test_backtest_returns_metrics(self, sample_data, params):
        m1, h1, d1 = sample_data
        result = backtest(m1, h1, d1, params)
        assert "trades" in result
        assert isinstance(result["trades"], int)

    def test_backtest_with_date_filter(self, sample_data, params):
        m1, h1, d1 = sample_data
        start = str(h1.index[100].date())
        end = str(h1.index[-100].date())
        result = backtest(m1, h1, d1, params, start=start, end=end)
        assert "trades" in result

    def test_backtest_zero_trades_has_defaults(self, sample_data):
        m1, h1, d1 = sample_data
        # Use extreme params that won't trigger any signals
        params = TrendPullBackParams(pullback_atr_ratio=0.001, breakout_lookback_bars=100)
        result = backtest(m1, h1, d1, params)
        assert result["trades"] == 0
        assert result["equity"] == 1.0

    def test_backtest_trade_details_present(self, sample_data, params):
        m1, h1, d1 = sample_data
        result = backtest(m1, h1, d1, params)
        if result["trades"] > 0:
            assert "trade_details" in result
            assert len(result["trade_details"]) == result["trades"]
            td = result["trade_details"][0]
            assert "entry_price" in td
            assert "exit_price" in td
