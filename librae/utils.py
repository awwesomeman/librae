"""Utility functions for backtest output construction and run ID generation.

Provides:
- build_backtest_output(): Convert engine BacktestResult → canonical BacktestOutput
- generate_run_id(): Deterministic-prefix run ID generation (re-exported from core.utils)
- metrics_dict_to_backtest_output(): Legacy dict adapter (deprecated)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from librae.backtest.engine import BacktestResult
from librae.backtest.schema import (
    BacktestOutput, EquityCurvePoint, RunMetadata, StrategyMetrics, TradeRecord,
    parse_utc_timestamp,
)
from librae.core.utils import generate_run_id  # noqa: F401 — re-exported


def build_backtest_output(
    result: BacktestResult,
    metrics: StrategyMetrics,
    *,
    run_id: str,
    strategy: str,
    symbol: str,
    timeframe: str,
    start_ts: datetime,
    end_ts: datetime,
    mode: str = "backtest",
    data_source: str = "binance",
    sample: str | None = None,
) -> BacktestOutput:
    """Convert engine BacktestResult + metrics into canonical BacktestOutput.

    Handles TradeResult→TradeRecord mapping, equity curve enhancement
    (drawdown, ret_1d), and benchmark alignment.
    """
    now = datetime.now(tz=timezone.utc)
    run_metadata = RunMetadata(
        run_id=run_id, strategy=strategy, symbol=symbol,
        timeframe=timeframe, start_ts=start_ts, end_ts=end_ts,
        run_ts=now, data_source=data_source, mode=mode, sample=sample,
    )

    trade_records = [
        TradeRecord(
            trade_id=f"{run_id}-t{i:04d}",
            entry_ts=t.entry_ts, exit_ts=t.exit_ts,
            symbol=symbol, side=t.side,
            entry_price=float(t.entry_price), exit_price=float(t.exit_price),
            quantity=float(t.quantity),
            gross_pnl=float(t.gross_pnl), net_pnl=float(t.net_pnl),
            gross_return=float(t.gross_return), net_return=float(t.net_return),
            commission=float(t.commission), slippage=float(t.slippage),
            holding_bars=int(t.holding_bars),
        )
        for i, t in enumerate(result.trades)
    ]

    has_bm = result.benchmark_curve is not None and len(result.benchmark_curve) > 0
    equity_points: list[EquityCurvePoint] = []
    peak = 0.0
    prev_eq = result.equity_curve[0].equity if result.equity_curve else 1.0
    prev_bm = 1.0
    for i, snap in enumerate(result.equity_curve):
        eq = snap.equity
        peak = max(peak, eq)
        drawdown = (eq - peak) / peak if peak > 0 else 0.0
        ret_1d = (eq / prev_eq - 1.0) if prev_eq > 0 else 0.0
        prev_eq = eq

        bm_eq, bm_ret = None, None
        if has_bm and i < len(result.benchmark_curve):
            bm_eq = float(result.benchmark_curve[i])
            bm_ret = (bm_eq / prev_bm - 1.0) if prev_bm > 0 else 0.0
            prev_bm = bm_eq

        equity_points.append(EquityCurvePoint(
            ts=snap.ts, equity=float(eq),
            ret_1d=float(ret_1d), drawdown=float(drawdown),
            benchmark_equity=bm_eq, benchmark_ret_1d=bm_ret,
        ))

    return BacktestOutput(
        run_metadata=run_metadata, equity_curve=equity_points,
        trades=trade_records, metrics=metrics,
    )


def metrics_dict_to_backtest_output(
    metrics: dict[str, Any],
    *,
    strategy: str,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    data_source: str = "unknown",
    run_id: str | None = None,
    sample: str | None = None,
) -> BacktestOutput:
    """Convert a legacy flat metrics dict to a BacktestOutput.

    Parameters
    ----------
    metrics : dict
        Output from legacy run_backtest(). Must contain at least ``trades``.
    strategy, symbol, timeframe : str
        Backtest context identifiers.
    start, end : str
        ISO date strings for the backtest window.
    data_source : str
        Origin of market data (e.g. "Shioaji", "Binance", "synthetic").
    run_id : str | None
        If None, one is auto-generated.
    sample : str | None
        Sample label (e.g. "train", "validation", "oos").

    Returns
    -------
    BacktestOutput
        Fully constructed output object. ``equity_curve`` and ``trades``
        are empty lists because the legacy interface doesn't track them.
    """
    if run_id is None:
        run_id = generate_run_id(strategy, symbol)

    now = datetime.now(tz=timezone.utc)
    start_dt = parse_utc_timestamp(start)
    end_dt = parse_utc_timestamp(end)

    run_metadata = RunMetadata(
        run_id=run_id,
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        start_ts=start_dt,
        end_ts=end_dt,
        run_ts=now,
        data_source=data_source,
        sample=sample,
    )

    n_trades = metrics.get("trades", 0)

    strategy_metrics = StrategyMetrics(
        total_return=metrics.get("equity", 1.0) - 1.0 if n_trades > 0 else 0.0,
        annual_return=metrics.get("ann_return", 0.0) or 0.0,
        sharpe=metrics.get("ann_sharpe", 0.0) or 0.0,
        max_drawdown=metrics.get("mdd", 0.0) or 0.0,
        win_rate=metrics.get("win_rate", 0.0) or 0.0,
        profit_factor=metrics.get("pf", 0.0) or 0.0,
        avg_trade_return=metrics.get("avg_ret", 0.0) or 0.0,
        trades=n_trades,
        exposure_ratio=0.0,
    )

    # Convert trade_details dicts to TradeRecord objects if present
    trade_records: list[TradeRecord] = []
    raw_trades = metrics.get("trade_details", [])
    for i, td in enumerate(raw_trades):
        entry_p = td["entry_price"]
        exit_p = td["exit_price"]
        pnl = exit_p - entry_p
        trade_records.append(TradeRecord(
            trade_id=f"{run_id}-t{i:04d}",
            entry_ts=start_dt,
            exit_ts=end_dt,
            symbol=symbol,
            side="buy",
            entry_price=entry_p,
            exit_price=exit_p,
            quantity=1.0,
            gross_pnl=pnl,
            net_pnl=pnl,
            holding_bars=td.get("bars_held"),
        ))

    return BacktestOutput(
        run_metadata=run_metadata,
        equity_curve=[],
        trades=trade_records,
        metrics=strategy_metrics,
    )
