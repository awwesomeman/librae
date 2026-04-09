"""Deterministic BTC H1 sample data for baseline backtests and regression tests.

This generates a reproducible OHLCV DataFrame using a fixed seed,
simulating ~30 days of hourly BTC data.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

SEED = 42
N_BARS = 720  # ~30 days of H1 bars
BASE_PRICE = 60_000.0


def generate_btc_h1_ohlcv(
    seed: int = SEED,
    n_bars: int = N_BARS,
    base_price: float = BASE_PRICE,
) -> pd.DataFrame:
    """Generate deterministic BTC H1 OHLCV data.

    Returns a DataFrame with columns: timestamp, open, high, low, close, volume.
    All values are reproducible given the same seed.
    """
    rng = np.random.default_rng(seed)

    returns = rng.normal(0.0002, 0.015, size=n_bars)
    close_prices = base_price * np.cumprod(1 + returns)

    intra_noise = rng.uniform(0.002, 0.008, size=n_bars)
    high_prices = close_prices * (1 + intra_noise)
    low_prices = close_prices * (1 - intra_noise)

    open_prices = np.empty(n_bars)
    open_prices[0] = base_price
    open_prices[1:] = close_prices[:-1]

    volume = rng.uniform(100, 2000, size=n_bars)

    start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    timestamps = pd.date_range(start=start, periods=n_bars, freq="h", tz=timezone.utc)

    return pd.DataFrame({
        "timestamp": timestamps,
        "open": np.round(open_prices, 2),
        "high": np.round(high_prices, 2),
        "low": np.round(low_prices, 2),
        "close": np.round(close_prices, 2),
        "volume": np.round(volume, 2),
    })


def run_simple_sma_crossover(
    df: pd.DataFrame,
    fast_period: int = 10,
    slow_period: int = 30,
) -> dict:
    """Deterministic SMA crossover backtest on OHLCV data.

    Returns a flat metrics dict compatible with the legacy backtest interface.
    """
    close = df["close"].values
    n = len(close)

    if n < slow_period:
        return {"trades": 0, "ann_return": 0, "ann_sharpe": 0, "mdd": 0}

    close_series = pd.Series(close)
    fast_sma = close_series.rolling(fast_period).mean().values
    slow_sma = close_series.rolling(slow_period).mean().values

    position = 0
    trades = []
    entry_price = 0.0
    entry_idx = 0

    for i in range(slow_period, n):
        if np.isnan(fast_sma[i]) or np.isnan(slow_sma[i]):
            continue
        if position == 0 and fast_sma[i] > slow_sma[i] and fast_sma[i - 1] <= slow_sma[i - 1]:
            position = 1
            entry_price = close[i]
            entry_idx = i
        elif position == 1 and fast_sma[i] < slow_sma[i] and fast_sma[i - 1] >= slow_sma[i - 1]:
            exit_price = close[i]
            pnl_points = exit_price - entry_price
            trades.append({
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl_points": pnl_points,
                "pnl_return": pnl_points / entry_price,
                "holding_periods": i - entry_idx,
            })
            position = 0

    if not trades:
        return {
            "trades": 0, "ann_return": 0.0, "ann_sharpe": 0.0,
            "mdd": 0.0, "pf": 0.0, "win_rate": 0.0,
            "avg_ret": 0.0, "avg_pnl_points": 0.0, "equity": 1.0,
        }

    returns = [t["pnl_return"] for t in trades]
    pnl_pts = [t["pnl_points"] for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]

    equity_curve = [1.0]
    for r in returns:
        equity_curve.append(equity_curve[-1] * (1 + r))

    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd

    total_return = equity_curve[-1] / equity_curve[0] - 1.0
    n_periods = n / (365.25 * 24)  # years of H1 data
    ann_return = (1 + total_return) ** (1 / n_periods) - 1 if n_periods > 0 else 0

    avg_ret = float(np.mean(returns))
    std_ret = float(np.std(returns, ddof=1)) if len(returns) > 1 else 1.0
    trades_per_year = len(trades) / n_periods if n_periods > 0 else 0
    ann_sharpe = (avg_ret / std_ret) * np.sqrt(trades_per_year) if std_ret > 0 else 0.0

    gross_profit = sum(r for r in returns if r > 0)
    gross_loss = abs(sum(r for r in returns if r <= 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    return {
        "trades": len(trades),
        "ann_return": float(ann_return),
        "ann_sharpe": float(ann_sharpe),
        "mdd": float(max_dd),
        "pf": float(pf),
        "win_rate": len(wins) / len(trades),
        "avg_ret": float(avg_ret),
        "avg_pnl_points": float(np.mean(pnl_pts)),
        "equity": float(equity_curve[-1]),
        "trade_details": trades,
    }
