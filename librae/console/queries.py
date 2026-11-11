"""SQL over the engine tables and the pure functions that shape its results.

Nothing here imports Streamlit, so every function can be tested without a
browser or a database. Each statement takes positional ``%s`` parameters.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from librae.core.metrics import SignalDirection, SignalPriceColumn, compute_signal_outcomes
from librae.core.run_config import HEARTBEAT_STALE_AFTER_POLLS
from librae.core.utils import interval_to_timedelta

# Result sizes are bounded. Full-run drawdown scans one account's equity
# history in the database and returns only its aggregate, every five minutes.
RECENT_RUNS = 200
HISTORY_ROWS = 50_000
RECENT_EVENTS = 1_000

RUNS = """
    SELECT run_id, strategy_name, mode, timeframe, run_at, poll_seconds, last_heartbeat_at
    FROM backtest_runs
    ORDER BY run_at DESC, run_id
    LIMIT %s
"""

SIGNAL_RUNS = """
    SELECT br.run_id, br.strategy_name, br.mode, br.timeframe, br.run_at,
           br.poll_seconds, br.last_heartbeat_at
    FROM backtest_runs br
    WHERE EXISTS (SELECT 1 FROM signal_events s WHERE s.run_id = br.run_id)
    ORDER BY br.run_at DESC, br.run_id
    LIMIT %s
"""

# Halt flag and last decision bar come from the live runtime's checkpoint, the
# state a restart resumes from; a backtest has no checkpoint row.
RUN_STATE = """
    SELECT br.mode, br.timeframe, br.poll_seconds, br.last_heartbeat_at,
           rs.halted, rs.last_decision_bar
    FROM backtest_runs br
    LEFT JOIN LATERAL (
      SELECT bool_or((state->>'halted')::boolean) AS halted,
             MAX((state->>'last_cycle_ts')::timestamptz) AS last_decision_bar
      FROM execution_runtime_state
      WHERE run_id = br.run_id
    ) rs ON true
    WHERE br.run_id = %s
"""

# The latest equity snapshot of each recent run's accounts and the positions
# open at that snapshot. Every account takes its own latest row, so one that
# stopped writing still shows, with its older timestamp. The accounts of a run
# are found by stepping through the (run_id, account_id, ts) index one account
# at a time, and each latest row comes from the same index, so nothing scans
# equity_curve. Here and in OPEN_POSITIONS, event order on one timestamp is
# (length, text) of event_id, whose numeric suffix makes "-10" follow "-9".
ACCOUNT_OVERVIEW = """
    WITH RECURSIVE recent_runs AS (
      SELECT run_id, strategy_name, mode, timeframe, poll_seconds, last_heartbeat_at
      FROM backtest_runs
      ORDER BY run_at DESC, run_id
      LIMIT %s
    ),
    run_accounts AS (
      SELECT r.run_id,
        (SELECT MIN(account_id) FROM equity_curve WHERE run_id = r.run_id) AS account_id
      FROM recent_runs r
      UNION ALL
      SELECT a.run_id,
        (SELECT MIN(account_id) FROM equity_curve
         WHERE run_id = a.run_id AND account_id > a.account_id)
      FROM run_accounts a
      WHERE a.account_id IS NOT NULL
    ),
    latest_equity AS (
      SELECT ec.run_id, ec.account_id, ec.currency, ec.ts, ec.equity,
        ec.gross_exposure, ec.net_exposure, ec.concentration
      FROM run_accounts a
      JOIN LATERAL (
        SELECT * FROM equity_curve
        WHERE run_id = a.run_id AND account_id = a.account_id
        ORDER BY ts DESC
        LIMIT 1
      ) ec ON true
    ),
    position_counts AS (
      SELECT le.run_id, le.account_id,
        COUNT(*) FILTER (WHERE p.remaining_quantity > 0) AS open_positions
      FROM latest_equity le
      LEFT JOIN LATERAL (
        SELECT DISTINCT ON (pe.symbol) pe.remaining_quantity
        FROM position_events pe
        WHERE pe.run_id = le.run_id
          AND pe.account_id = le.account_id
          AND pe.ts <= le.ts
        ORDER BY pe.symbol, pe.ts DESC, length(pe.event_id) DESC, pe.event_id DESC
      ) p ON true
      GROUP BY le.run_id, le.account_id
    )
    SELECT r.strategy_name, r.mode, r.timeframe, r.run_id, le.account_id, le.currency,
           le.ts AS last_equity_at, le.equity, le.gross_exposure, le.net_exposure,
           le.concentration, pc.open_positions, r.poll_seconds, r.last_heartbeat_at,
           rs.halted
    FROM recent_runs r
    LEFT JOIN latest_equity le ON le.run_id = r.run_id
    LEFT JOIN position_counts pc ON pc.run_id = le.run_id AND pc.account_id = le.account_id
    LEFT JOIN LATERAL (
      SELECT bool_or((state->>'halted')::boolean) AS halted
      FROM execution_runtime_state
      WHERE run_id = r.run_id
    ) rs ON true
    ORDER BY le.ts DESC NULLS LAST, r.strategy_name, r.run_id
"""

RUN_ACCOUNTS = """
    SELECT DISTINCT account_id, currency
    FROM equity_curve
    WHERE run_id = %s
    ORDER BY account_id
"""

PERFORMANCE = """
    SELECT currency, net_pnl, total_return, max_drawdown, period_sharpe, period_sortino,
           win_rate, profit_factor, trades
    FROM strategy_performance
    WHERE run_id = %s AND account_id = %s
"""

EQUITY = """
    SELECT ts, equity, gross_exposure, net_exposure, concentration
    FROM equity_curve
    WHERE run_id = %s AND account_id = %s
    ORDER BY ts DESC
    LIMIT %s
"""

LATEST_EQUITY = """
    SELECT ts, equity
    FROM equity_curve
    WHERE run_id = %s AND account_id = %s
    ORDER BY ts DESC
    LIMIT 1
"""

EQUITY_DRAWDOWN = """
    WITH curve AS (
      SELECT equity, MAX(equity) OVER (ORDER BY ts) AS peak
      FROM equity_curve
      WHERE run_id = %s AND account_id = %s
    )
    SELECT MIN(equity - peak) AS max_drawdown_amount,
           MIN(equity / NULLIF(peak, 0) - 1) AS max_drawdown
    FROM curve
"""

OPEN_POSITIONS = """
    SELECT *
    FROM (
      SELECT DISTINCT ON (symbol)
        symbol, side, remaining_quantity, entry_price, entry_at, price, fill_quantity,
        notional, margin_locked, leverage, liquidation_price, margin_mode
      FROM position_events
      WHERE run_id = %s AND account_id = %s
      ORDER BY symbol, ts DESC, length(event_id) DESC, event_id DESC
    ) latest
    WHERE remaining_quantity > 0
"""

POSITION_EVENTS = """
    SELECT ts, symbol, event_type, side, fill_quantity, price, notional, cash_flow,
           realized_pnl, net_return, margin_roi, margin_mode, entry_price, entry_at,
           remaining_quantity, commission, slippage, tax, group_id, periods_held, reason
    FROM position_events
    WHERE run_id = %s AND account_id = %s
    ORDER BY ts DESC, length(event_id) DESC, event_id DESC
    LIMIT %s
"""

# run_bars keeps every version of a bar it received; a run used the last one.
# Bars a backtest stored have no observed_at and fall back to available_at.
LATEST_MARKS = """
    SELECT DISTINCT ON (symbol) symbol, ts, close
    FROM run_bars
    WHERE run_id = %s
    ORDER BY symbol, ts DESC, observed_at DESC NULLS LAST, available_at DESC
"""

RUN_BARS = """
    SELECT DISTINCT ON (ts) ts, open, high, low, close
    FROM run_bars
    WHERE run_id = %s AND symbol = %s
    ORDER BY ts DESC, observed_at DESC NULLS LAST, available_at DESC
    LIMIT %s
"""

BAR_SYMBOLS = """
    SELECT DISTINCT symbol
    FROM run_bars
    WHERE run_id = %s
    ORDER BY symbol
"""

SIGNAL_SYMBOLS = """
    SELECT DISTINCT symbol
    FROM signal_events
    WHERE run_id = %s AND signal_type = %s
    ORDER BY symbol
"""

SIGNALS = """
    SELECT ts, signal_value
    FROM signal_events
    WHERE run_id = %s AND signal_type = %s AND symbol = %s
    ORDER BY ts DESC
    LIMIT %s
"""

SIGNAL_SUMMARY = """
    WITH recent AS (
      SELECT ts, signal_value
      FROM signal_events
      WHERE run_id = %s AND signal_type = %s AND symbol = %s
      ORDER BY ts DESC
      LIMIT %s
    )
    SELECT COUNT(*) AS signal_count, MAX(ts) AS last_ts,
           (ARRAY_AGG(signal_value ORDER BY ts DESC))[1] AS last_value
    FROM recent
"""

SIGNAL_REFERENCE = """
    SELECT ts, open, high, low, close
    FROM run_bars
    WHERE run_id = %s AND symbol = %s AND ts > %s
    ORDER BY ts ASC, observed_at DESC NULLS LAST, available_at DESC
    LIMIT 1
"""

RUNTIME_EVENTS = """
    SELECT ts, event_type, symbol, detail->>'reason' AS reason, detail::text AS detail
    FROM runtime_events
    WHERE run_id = %s
    ORDER BY ts DESC
    LIMIT %s
"""


def oldest_first(frame: pd.DataFrame) -> pd.DataFrame:
    """A newest-first, row-limited result back in time order."""
    return frame.iloc[::-1].reset_index(drop=True)


def trade_returns(events: pd.DataFrame) -> pd.Series:
    """Net return per closed trade, grouped the way ``strategy_performance`` counts trades.

    Exits sharing a ``group_id`` and ``entry_at`` (the legs of one hedge) are
    one trade: their realized P&L over their summed entry notional. Exits
    without a group keep their own stored return. Financing is not included.
    """
    exits = events[events["net_return"].notna()]
    if exits.empty:
        return pd.Series(dtype=float)
    single = exits[exits["group_id"].isna()]["net_return"] / 100.0
    grouped = exits[exits["group_id"].notna()]
    if grouped.empty:
        return single.reset_index(drop=True)
    entry_notional = (grouped["notional"] * grouped["entry_price"] / grouped["price"]).abs()
    sums = (
        grouped.assign(entry_notional=entry_notional)
        .groupby(["group_id", "entry_at"])[["realized_pnl", "entry_notional"]]
        .sum()
    )
    combined = sums["realized_pnl"] / sums["entry_notional"].replace(0, np.nan)
    return pd.concat([single, combined], ignore_index=True)


def run_status(
    last_heartbeat_at: datetime | None, poll_seconds: float | None, now: datetime
) -> str:
    """``running``, ``stale`` (missed heartbeats), or ``not polling`` (a backtest)."""
    if pd.isna(last_heartbeat_at) or pd.isna(poll_seconds) or poll_seconds <= 0:
        return "not polling"
    stale_after = pd.Timedelta(seconds=float(poll_seconds) * HEARTBEAT_STALE_AFTER_POLLS)
    return "running" if last_heartbeat_at >= now - stale_after else "stale"


def is_behind(
    latest: datetime | None, timeframe: str, poll_seconds: float | None, now: datetime
) -> bool:
    """Whether ``latest``, a bar's start, is older than the run should ever let it get.

    The next bar completes two bar lengths after the start of this one, and a
    polling run notices it within three poll cycles. Market closures are not
    modelled, so a run on a session calendar reads as behind while closed.
    """
    if latest is None or pd.isna(latest):
        return False
    allowance = 2 * interval_to_timedelta(timeframe)
    if not pd.isna(poll_seconds):
        allowance += pd.Timedelta(seconds=float(poll_seconds) * HEARTBEAT_STALE_AFTER_POLLS)
    return pd.Timestamp(now) - pd.Timestamp(latest) > allowance


def format_age(age: pd.Timedelta) -> str:
    seconds = max(age.total_seconds(), 0.0)
    if seconds < 120:
        return f"{seconds:.0f} s"
    if seconds < 2 * 3600:
        return f"{seconds / 60:.0f} min"
    if seconds < 2 * 86400:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.0f} d"


def with_status(frame: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Add ``status`` and ``behind`` for the runs overview.

    ``status`` is ``halted`` when the checkpoint says so, otherwise the
    heartbeat status; ``behind`` flags a polling run whose latest equity
    point is older than its timeframe allows.
    """
    out = frame.copy()
    rows = zip(
        out["last_heartbeat_at"],
        out["poll_seconds"],
        out["halted"],
        out["last_equity_at"],
        out["timeframe"],
        strict=True,
    )
    status, behind = [], []
    for heartbeat, poll, halted, latest, timeframe in rows:
        state = run_status(heartbeat, poll, now)
        polling = state != "not polling"
        status.append("halted" if polling and not pd.isna(halted) and bool(halted) else state)
        behind.append(polling and is_behind(latest, timeframe, poll, now))
    out["status"] = status
    out["behind"] = behind
    return out


def with_drawdown(equity: pd.DataFrame) -> pd.DataFrame:
    """Add ``drawdown``: equity relative to its running peak, 0 at a new high."""
    out = equity.copy()
    values = out["equity"].astype(float)
    out["drawdown"] = values / values.cummax() - 1.0
    return out


def position_snapshot(
    positions: pd.DataFrame, marks: pd.DataFrame, equity: float | None
) -> pd.DataFrame:
    """Mark open positions to the run's latest stored bar.

    The contract multiplier is recovered from the symbol's latest event:
    ``notional = price * fill_quantity * multiplier``. A symbol without a
    stored bar keeps an empty mark rather than a guessed one.
    """
    columns = [
        "symbol",
        "side",
        "remaining_quantity",
        "entry_price",
        "market_price",
        "mtm_pnl",
        "mtm_return",
        "weight",
        "margin_locked",
        "leverage",
        "margin_mode",
        "liquidation_price",
        "liquidation_buffer",
        "entry_at",
    ]
    if positions.empty:
        return pd.DataFrame(columns=columns)
    out = positions.merge(
        marks[["symbol", "close"]].rename(columns={"close": "market_price"}),
        on="symbol",
        how="left",
    )
    sign = np.where(out["side"] == "long", 1.0, -1.0)
    multiplier = out["notional"] / (out["price"] * out["fill_quantity"]).replace(0, np.nan)
    move = sign * (out["market_price"] - out["entry_price"])
    out["mtm_pnl"] = move * out["remaining_quantity"] * multiplier
    out["mtm_return"] = move / out["entry_price"].replace(0, np.nan)
    signed_notional = sign * out["remaining_quantity"] * out["market_price"] * multiplier
    out["weight"] = signed_notional / equity if equity else np.nan
    out["liquidation_buffer"] = (sign * (out["market_price"] - out["liquidation_price"])) / out[
        "market_price"
    ].replace(0, np.nan)
    out = out.reindex(out["weight"].abs().sort_values(ascending=False, na_position="last").index)
    return out[columns].reset_index(drop=True)


def complete_total(values: pd.Series) -> float:
    """Zero for a flat account; unknown if any open position is missing a value."""
    return float("nan") if values.isna().any() else float(values.sum())


def margin_breakdown(snapshot: pd.DataFrame, equity: float | None) -> pd.DataFrame:
    """Margin by engine regime, preserving unknown amounts and unlabelled modes."""
    grouped = (
        snapshot.assign(margin_mode=snapshot["margin_mode"].fillna("Unknown"))
        .groupby("margin_mode")["margin_locked"]
        .agg(complete_total)
        .rename("amount")
        .reset_index()
    )
    grouped["share"] = grouped["amount"] / equity if equity and equity > 0 else np.nan
    return grouped


def fill_markers(events: pd.DataFrame) -> pd.DataFrame:
    """Fills as chart markers at their execution time and price, labelled by direction."""
    return pd.DataFrame(
        {
            "ts": events["ts"],
            "price": events["price"],
            "label": [
                f"{'Entry' if kind in ('open', 'add') else 'Exit'} {side}"
                for kind, side in zip(events["event_type"], events["side"], strict=True)
            ],
        }
    )


def signal_markers(signals: pd.DataFrame, bars: pd.DataFrame, label: str) -> pd.DataFrame:
    """Signals as chart markers at the close of the latest bar at or before each signal."""
    if signals.empty or bars.empty:
        return pd.DataFrame(columns=["ts", "price", "label"])
    placed = pd.merge_asof(
        signals[["ts"]].sort_values("ts"),
        bars[["ts", "close"]].sort_values("ts"),
        on="ts",
        direction="backward",
    )
    return pd.DataFrame({"ts": placed["ts"], "price": placed["close"], "label": label})


def signal_outcomes_at(
    signal_ts: pd.Series,
    bars: pd.DataFrame,
    horizon: int,
    direction: SignalDirection,
    price_col: SignalPriceColumn,
) -> pd.DataFrame:
    """Per-signal forward return, MFE and MAE at ``horizon`` bars, as fractions.

    Delegates to ``compute_signal_outcomes``, so the console and a Python
    report agree on the reference bar and the offsets. Signals without
    ``horizon`` bars after their reference bar, or older than the first bar
    loaded, are left out.
    """
    columns = ["ts", "forward_return", "mfe", "mae"]
    if not bars.empty:
        signal_ts = signal_ts[signal_ts >= bars["ts"].iloc[0]]
    if signal_ts.empty or bars.empty:
        return pd.DataFrame(columns=columns)
    outcomes = compute_signal_outcomes(
        list(signal_ts),
        bars.set_index("ts"),
        max_periods=horizon,
        direction=direction,
        price_col=price_col,
    )
    at = outcomes[outcomes["bar_offset"] == horizon]
    out = at[columns].copy()
    out[["forward_return", "mfe", "mae"]] = out[["forward_return", "mfe", "mae"]] / 100.0
    return out.sort_values("ts").reset_index(drop=True)


def rolling_signal_stats(outcomes: pd.DataFrame, window: int) -> pd.DataFrame:
    """Cumulative return, rolling mean return and rolling edge ratio (mean MFE / mean MAE)."""
    out = outcomes[["ts"]].copy()
    out["cumulative_return"] = outcomes["forward_return"].cumsum()
    out["rolling_mean_return"] = outcomes["forward_return"].rolling(window).mean()
    mean_mae = outcomes["mae"].rolling(window).mean().replace(0, np.nan)
    out["rolling_edge_ratio"] = outcomes["mfe"].rolling(window).mean() / mean_mae
    return out


def edge_ratio(outcomes: pd.DataFrame) -> float:
    """Mean MFE over mean MAE; NaN when there is no adverse excursion to divide by."""
    mean_mae = outcomes["mae"].mean()
    return float(outcomes["mfe"].mean() / mean_mae) if mean_mae else float("nan")


def latest_signal_return(
    signal_ts: pd.Series,
    bars: pd.DataFrame,
    direction: SignalDirection,
    price_col: SignalPriceColumn,
) -> float | None:
    """Direction-adjusted return from the latest signal's reference price to the last close.

    The reference price is ``price_col`` of the first bar after the signal,
    the same convention ``signal_outcomes_at`` uses. None when no bar
    follows the signal yet.
    """
    if signal_ts.empty or bars.empty:
        return None
    after = bars[bars["ts"] > signal_ts.max()]
    if after.empty:
        return None
    return signal_return(after.iloc[0][price_col], bars.iloc[-1]["close"], direction)


def signal_return(
    reference: float | None, latest_close: float | None, direction: SignalDirection
) -> float | None:
    """Return from a known reference; unavailable prices stay unknown."""
    if pd.isna(reference) or pd.isna(latest_close) or reference == 0:
        return None
    sign = 1.0 if direction == "long" else -1.0
    return sign * (float(latest_close) - float(reference)) / float(reference)
