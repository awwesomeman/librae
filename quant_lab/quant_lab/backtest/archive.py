"""Parquet archiving for backtest outputs.

Saves equity curve and trade log as Parquet files under:
    <base_dir>/archive/{run_id}/equity_curve.parquet
    <base_dir>/archive/{run_id}/trades.parquet

Skills: python, quant
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from .schema import BacktestOutput


def archive_backtest_parquet(
    output: "BacktestOutput",
    base_dir: str | Path,
) -> dict[str, Path]:
    """Persist equity curve and trades as Parquet files.

    Parameters
    ----------
    output : BacktestOutput
        Validated backtest output object.
    base_dir : str | Path
        Root data directory. Files land under ``base_dir/archive/{run_id}/``.

    Returns
    -------
    dict with keys ``equity_curve`` and ``trades`` mapping to written paths.
    """
    run_id = output.run_metadata.run_id
    archive_dir = Path(base_dir) / "archive" / run_id
    archive_dir.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path] = {}

    # --- Equity curve ---
    if output.equity_curve:
        eq_records = [asdict(pt) for pt in output.equity_curve]
        eq_df = pd.DataFrame(eq_records)
        # Ensure ts is proper datetime
        if "ts" in eq_df.columns:
            eq_df["ts"] = pd.to_datetime(eq_df["ts"], utc=True)
    else:
        eq_df = pd.DataFrame(
            columns=["ts", "equity", "equity_unit", "ret_1d", "drawdown",
                      "benchmark_equity", "benchmark_ret_1d"]
        )

    eq_path = archive_dir / "equity_curve.parquet"
    eq_df.to_parquet(eq_path, index=False, engine="pyarrow")
    paths["equity_curve"] = eq_path

    # --- Trades ---
    if output.trades:
        trade_records = [asdict(tr) for tr in output.trades]
        trades_df = pd.DataFrame(trade_records)
        for col in ("entry_ts", "exit_ts"):
            if col in trades_df.columns:
                trades_df[col] = pd.to_datetime(trades_df[col], utc=True)
    else:
        trades_df = pd.DataFrame(
            columns=["trade_id", "entry_ts", "exit_ts", "symbol", "side",
                      "entry_price", "exit_price", "quantity", "price_unit",
                      "quantity_unit", "gross_pnl", "net_pnl", "pnl_unit",
                      "commission", "commission_unit", "slippage",
                      "slippage_unit", "holding_bars"]
        )

    trades_path = archive_dir / "trades.parquet"
    trades_df.to_parquet(trades_path, index=False, engine="pyarrow")
    paths["trades"] = trades_path

    return paths
