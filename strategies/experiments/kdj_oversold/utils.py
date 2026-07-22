"""KDJ Oversold — signal computation.

Level-based oversold signal for signal-quality research (forward-return
predictive power via save_signal_results/factrix), not position-managed
backtesting — entry_signal is 1 on every bar where KDJ's J line is below
j_threshold, not a single discrete crossover event. See run.py.
"""
from __future__ import annotations

import pandas as pd
import pandas_ta_classic as ta

SIGNAL_NAME = "kdj_oversold"

DEFAULT_PARAMS: dict = {
    "kdj_length": 9,
    "kdj_signal": 3,
    "j_threshold": 20,
    # Below: only read by factor_research.py's backtest candidate — run.py's
    # DB-signal-quality path (prepare_signals) never touches these.
    "exit_j_threshold": 80,
    "max_hold_periods": 24,
}


def prepare_signals(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Add KDJ columns + entry_signal to an OHLCV DataFrame.

    Expects df with DatetimeIndex and open/high/low/close columns.
    Returns a copy with kdj_k/kdj_d/kdj_j + entry_signal (1 = J below
    j_threshold, i.e. oversold; 0 otherwise).
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    out = df.copy()

    kdj = ta.kdj(
        out["high"], out["low"], out["close"],
        length=p["kdj_length"], signal=p["kdj_signal"],
    )
    out["kdj_k"] = kdj[f"K_{p['kdj_length']}_{p['kdj_signal']}"]
    out["kdj_d"] = kdj[f"D_{p['kdj_length']}_{p['kdj_signal']}"]
    out["kdj_j"] = kdj[f"J_{p['kdj_length']}_{p['kdj_signal']}"]
    out["entry_signal"] = (out["kdj_j"] < p["j_threshold"]).astype(int)
    return out


def compute_exit_signal(df: pd.DataFrame, params: dict | None = None) -> pd.Series:
    """Mean-reversion exit: J recovered back above exit_j_threshold. Used
    only by factor_research.py's backtest candidate (see run.py's
    DB-signal-quality path — it has no exit concept). max_hold_periods is
    a forced exit enforced by the Strategy, not here."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    return (df["kdj_j"] > p["exit_j_threshold"]).fillna(False)
