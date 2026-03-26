from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from influxdb_client import Point

from quant_lab.backtest.schema import BacktestOutput
from quant_lab.backtest.metrics import compute_all
from quant_lab.contracts import SCHEMA_VERSION


def _now() -> datetime:
    return datetime.now(timezone.utc)


def points_from_backtest(output: BacktestOutput, sample: str = "oos", benchmark: str = "TWSE") -> list[Point]:
    meta = output.run_metadata
    points: list[Point] = []

    for tr in output.trades:
        side = str(tr.side).lower()
        signal_strength = 1.0 if side in {"buy", "long"} else -1.0
        # Entry point
        points.append(
            Point("strategy_signals")
            .tag("schema_version", meta.schema_version or SCHEMA_VERSION)
            .tag("strategy", meta.strategy)
            .tag("symbol", meta.symbol)
            .tag("timeframe", meta.timeframe)
            .tag("side", side)
            .tag("source", meta.data_source)
            .tag("run_id", meta.run_id)
            .tag("signal_type", "entry")
            .field("signal_strength", float(signal_strength))
            .field("confidence", 0.5)
            .field("price", float(tr.entry_price))
            .field("quantity", float(tr.quantity))
            .time(tr.entry_ts)
        )
        # Exit point
        points.append(
            Point("strategy_signals")
            .tag("schema_version", meta.schema_version or SCHEMA_VERSION)
            .tag("strategy", meta.strategy)
            .tag("symbol", meta.symbol)
            .tag("timeframe", meta.timeframe)
            .tag("side", side)
            .tag("source", meta.data_source)
            .tag("run_id", meta.run_id)
            .tag("signal_type", "exit")
            .field("signal_strength", float(-signal_strength))
            .field("confidence", 0.5)
            .field("price", float(tr.exit_price))
            .field("quantity", float(tr.quantity))
            .field("net_pnl", float(tr.net_pnl))
            .time(tr.exit_ts)
        )

    m = output.metrics
    # Recompute metrics from the metrics module for consistency
    computed = compute_all(output)

    perf_point = (
        Point("strategy_performance")
        .tag("schema_version", meta.schema_version or SCHEMA_VERSION)
        .tag("strategy", meta.strategy)
        .tag("symbol", meta.symbol)
        .tag("timeframe", meta.timeframe)
        .tag("run_id", meta.run_id)
        .tag("sample", sample)
        .tag("benchmark", benchmark)
        .field("total_return", float(m.total_return))
        .field("annual_return", float(m.annual_return))
        .field("sharpe", float(m.sharpe))
        .field("max_drawdown", float(m.max_drawdown))
        .field("win_rate", float(m.win_rate))
        .field("trades", int(m.trades))
    )
    # Add all computed metrics (sortino, calmar, payoff_ratio, expectancy, etc.)
    for metric_name, result in computed.items():
        if metric_name not in {"sharpe", "max_drawdown", "win_rate", "total_return", "annual_return"}:
            perf_point = perf_point.field(metric_name, float(result.value))
    # Add required fields not covered by compute_all
    perf_point = perf_point.field("avg_trade_return", float(m.avg_trade_return))
    perf_point = perf_point.field("exposure_ratio", float(m.exposure_ratio))
    perf_point = perf_point.field("bh_total_return", float(m.bh_total_return))
    perf_point = perf_point.field("profit_factor", float(m.profit_factor))
    perf_point = perf_point.time(meta.run_ts)
    points.append(perf_point)

    for eq in output.equity_curve:
        points.append(
            Point("perf_equity_curve")
            .tag("schema_version", meta.schema_version or SCHEMA_VERSION)
            .tag("strategy", meta.strategy)
            .tag("symbol", meta.symbol)
            .tag("timeframe", meta.timeframe)
            .tag("run_id", meta.run_id)
            .tag("sample", sample)
            .tag("benchmark", benchmark)
            .field("equity", float(eq.equity))
            .field("ret_1d", float(eq.ret_1d))
            .field("drawdown", float(eq.drawdown))
            .field("benchmark_equity", float(eq.benchmark_equity if eq.benchmark_equity is not None else eq.equity))
            .field("benchmark_ret_1d", float(eq.benchmark_ret_1d if eq.benchmark_ret_1d is not None else 0.0))
            .time(eq.ts)
        )

    for tr in output.trades:
        points.append(
            Point("trade_blotter")
            .tag("schema_version", meta.schema_version or SCHEMA_VERSION)
            .tag("strategy", meta.strategy)
            .tag("symbol", meta.symbol)
            .tag("run_id", meta.run_id)
            .tag("side", str(tr.side).lower())
            .field("entry_price", float(tr.entry_price))
            .field("exit_price", float(tr.exit_price))
            .field("quantity", float(tr.quantity))
            .field("net_pnl", float(tr.net_pnl))
            .field("holding_bars", int(tr.holding_bars or 0))
            .field("entry_time", tr.entry_ts.isoformat() if tr.entry_ts else "")
            .time(tr.exit_ts)
        )

    pnl_values = [float(t.net_pnl) for t in output.trades]
    if pnl_values:
        wins = [x for x in pnl_values if x > 0]
        losses = [x for x in pnl_values if x <= 0]
        points.append(
            Point("trade_distribution")
            .tag("schema_version", meta.schema_version or SCHEMA_VERSION)
            .tag("strategy", meta.strategy)
            .tag("symbol", meta.symbol)
            .tag("run_id", meta.run_id)
            .field("count", int(len(pnl_values)))
            .field("win_count", int(len(wins)))
            .field("loss_count", int(len(losses)))
            .field("avg_win", float(sum(wins) / len(wins) if wins else 0.0))
            .field("avg_loss", float(sum(losses) / len(losses) if losses else 0.0))
            .time(meta.run_ts)
        )

    return points


def points_from_ohlcv(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    run_id: str,
    source: str = "backtest",
) -> list[Point]:
    """Convert an OHLCV DataFrame (DatetimeIndex) to InfluxDB points.

    Measurement: ``ohlcv``
    Tags: symbol, timeframe, source, run_id
    Fields: open, high, low, close, volume (all float)
    """
    pts: list[Point] = []
    for ts, row in df.iterrows():
        pt = (
            Point("ohlcv")
            .tag("symbol", symbol)
            .tag("timeframe", timeframe)
            .tag("source", source)
            .tag("run_id", run_id)
            .field("open", float(row["open"]))
            .field("high", float(row["high"]))
            .field("low", float(row["low"]))
            .field("close", float(row["close"]))
            .field("volume", float(row.get("volume", 0.0)))
            .time(ts)
        )
        pts.append(pt)
    return pts
