"""Real-time signal monitor — bridges signal_engine into the monitoring pipeline.

Accepts live OHLCV data via a duck-typed adapter, runs TrendPullback signal
generation, and produces Points for downstream consumers (TimescaleDB writer).

Skills: python, quant
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import pandas as pd
from influxdb_client import Point

from quant_lab.contracts import SCHEMA_VERSION
from quant_lab.signal_engine.trendpullback import (
    compute_features,
    compute_daily_gate,
    generate_signals,
    resample_to_daily,
    DEFAULT_PARAMS,
)


@runtime_checkable
class OHLCVAdapter(Protocol):
    """Duck-typed adapter — any object with ``fetch_ohlcv`` qualifies."""

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame: ...


def signal_to_point(
    signal: int,
    confidence: float,
    price: float,
    ts: datetime,
    *,
    strategy: str = "TrendPullback",
    symbol: str = "UNKNOWN",
    timeframe: str = "1h",
    source: str = "live",
    run_id: str = "monitor",
) -> Point:
    """Format a single signal into a ``strategy_signals`` Point.
        schema_version, strategy, symbol, timeframe, side, source, run_id, signal_type.

    Fields: signal_strength (float), confidence (float), price (float).
    """
    side = "long" if signal >= 1 else "exit" if signal <= -1 else "flat"
    signal_type = "entry" if signal >= 1 else "exit" if signal <= -1 else "hold"

    return (
        Point("strategy_signals")
        .tag("schema_version", SCHEMA_VERSION)
        .tag("strategy", strategy)
        .tag("symbol", symbol)
        .tag("timeframe", timeframe)
        .tag("side", side)
        .tag("source", source)
        .tag("run_id", run_id)
        .tag("signal_type", signal_type)
        .field("signal_strength", float(signal))
        .field("confidence", float(confidence))
        .field("price", float(price))
        .time(ts)
    )


def _prepare_dataframe(
    df: pd.DataFrame,
    params: dict | None = None,
    daily_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Add features + daily trend gate to raw OHLCV.

    Parameters
    ----------
    daily_df : pd.DataFrame | None
        Pre-fetched D1 OHLCV.  When provided the function uses it directly
        instead of resampling from H1 data.
    """
    p = {**DEFAULT_PARAMS, **(params or {})}

    # Ensure datetime index
    if "ts" in df.columns:
        out = df.set_index("ts").copy()
    else:
        out = df.copy()
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index, utc=True)

    # H1 features
    out = compute_features(out, p)

    # Daily gate — use supplied D1 data or fall back to resample
    if daily_df is not None:
        daily = daily_df.copy()
        if "ts" in daily.columns:
            daily = daily.set_index("ts")
        if not isinstance(daily.index, pd.DatetimeIndex):
            daily.index = pd.to_datetime(daily.index, utc=True)
    else:
        daily = resample_to_daily(out)

    daily = compute_daily_gate(daily, p)
    daily["daily_trend"] = daily["ema20"] > daily["ema20_prev"]

    # Map daily trend back to intraday bars
    out["_date"] = out.index.date
    trend_map = daily["daily_trend"].to_dict()  # date → bool
    out["daily_trend"] = out["_date"].map(
        lambda d: trend_map.get(d, False)
    )
    out.drop(columns=["_date"], inplace=True)

    return out


def run_monitor(
    adapter: Any,
    symbol: str,
    timeframe: str = "1h",
    params: dict | None = None,
    *,
    limit: int = 200,
    source: str = "live",
    run_id: str = "monitor",
    daily_df: pd.DataFrame | None = None,
) -> Point | None:
    """Fetch latest OHLCV, generate signals, return an InfluxDB Point.

    Parameters
    ----------
    adapter : duck-typed
        Must implement ``fetch_ohlcv(symbol, timeframe, limit) -> pd.DataFrame``
        returning columns: ts, open, high, low, close, volume.
    symbol : str
        Trading pair / instrument, e.g. ``"BTC/USDT"``.
    timeframe : str
        Candle interval, e.g. ``"1h"``.
    params : dict, optional
        Override ``DEFAULT_PARAMS`` for the signal engine.
    limit : int
        Number of bars to request from the adapter (default 200).
    source : str
        Tag value for the InfluxDB point (default ``"live"``).
    run_id : str
        Tag value for the InfluxDB point (default ``"monitor"``).
    daily_df : pd.DataFrame | None
        Pre-fetched D1 OHLCV for the daily trend gate.  When ``None``
        the adapter is used to fetch D1 data automatically.

    Returns
    -------
    Point or None
        An ``influxdb_client.Point`` with measurement ``strategy_signals``.
        Returns ``None`` when the adapter returns an empty DataFrame.

    Output Point schema
    -------------------
    - measurement: ``strategy_signals``
    - tags: schema_version, strategy, symbol, timeframe, side, source, run_id, signal_type
    - fields: signal_strength (float), confidence (float), price (float)
    - time: timestamp of the latest bar
    """
    raw = adapter.fetch_ohlcv(symbol, timeframe, limit)
    if raw is None or raw.empty:
        return None

    # Fetch D1 data for daily gate if not supplied
    if daily_df is None:
        try:
            daily_df = adapter.fetch_ohlcv(symbol, "1d", 60)
        except Exception:
            daily_df = None  # fall back to resample inside _prepare_dataframe

    prepared = _prepare_dataframe(raw, params, daily_df=daily_df)
    sig_df = generate_signals(prepared, params)

    # Latest bar
    last_idx = sig_df.index[-1]
    signal_val = int(sig_df.iloc[-1]["signal"])

    # Confidence: binary — 1.0 when any signal present, 0.0 for hold
    confidence = 1.0 if signal_val != 0 else 0.0
    price = float(prepared.loc[last_idx, "close"])
    ts = last_idx if isinstance(last_idx, datetime) else pd.Timestamp(last_idx).to_pydatetime()

    return signal_to_point(
        signal=signal_val,
        confidence=confidence,
        price=price,
        ts=ts,
        strategy="TrendPullback",
        symbol=symbol,
        timeframe=timeframe,
        source=source,
        run_id=run_id,
    )
