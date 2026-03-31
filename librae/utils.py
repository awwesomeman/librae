"""Legacy adapter utilities.

Provides:
- metrics_dict_to_backtest_output(): Convert legacy flat metrics dict → BacktestOutput
- generate_run_id(): Re-exported from core.utils for backward compat
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from librae.backtest.schema import (
    BacktestOutput, RunMetadata, StrategyMetrics, TradeRecord,
    parse_utc_timestamp,
)
from librae.core.utils import generate_run_id, make_trade_id


def metrics_dict_to_backtest_output(
    metrics: dict[str, Any],
    *,
    strategy: str,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    run_id: str | None = None,
    **_kwargs: Any,
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
    run_id : str | None
        If None, one is auto-generated.
    **_kwargs
        Accepts (and ignores) legacy keyword arguments such as
        ``data_source`` and ``sample`` for backward compatibility.

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
            trade_id=make_trade_id(run_id, i),
            entry_ts=start_dt,
            exit_ts=end_dt,
            symbol=symbol,
            side="long",
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
