"""A run's raw signals against the bars it stored, with their forward outcomes."""

from __future__ import annotations

import math

import pandas as pd
import streamlit as st

from librae.console import charts, common, db, queries


def _pct(value: float | None) -> str:
    return "–" if value is None or math.isnan(value) else f"{value:.2%}"


def render() -> None:
    st.header("Signals")
    selected = common.selection()
    if selected is None:
        return
    common.status_panel(selected.run["run_id"])
    st.caption("Signal analysis is not auto-refreshed: reload the page to redraw.")
    _body(selected)


def _body(selected: common.Selection) -> None:
    run_id = selected.run["run_id"]
    controls = st.columns(4)
    signal_type = (
        controls[0].segmented_control(
            "Signal", ["entry", "exit"], default="entry", key="librae_signal_type"
        )
        or "entry"
    )
    direction = (
        controls[1].segmented_control(
            "Expected direction",
            ["long", "short"],
            default="long",
            key="librae_signal_direction",
            help="The price move the signal predicts; independent of entry or exit.",
        )
        or "long"
    )
    price_col = controls[2].selectbox(
        "Reference price",
        ["close", "open", "high", "low"],
        key="librae_signal_price",
        help="Field of the first bar after the signal that returns are measured from.",
    )
    symbols = db.query(queries.SIGNAL_SYMBOLS, (run_id, signal_type))["symbol"].tolist()
    if not symbols:
        st.info(f"This run recorded no {signal_type} signals.")
        return
    symbol = controls[3].selectbox("Symbol", symbols, key="librae_signal_symbol")
    horizon, window = st.columns(2)
    n = horizon.number_input("Horizon (bars)", min_value=1, value=24, key="librae_signal_n")
    k = window.number_input(
        "Rolling window (signals)", min_value=2, value=50, key="librae_signal_k"
    )

    signals = common.history(queries.SIGNALS, (run_id, signal_type, symbol))
    bars = common.history(queries.RUN_BARS, (run_id, symbol))
    last_ts = signals["ts"].max()
    age = common.now() - pd.Timestamp(last_ts)

    cols = st.columns(4)
    cols[0].metric("Signals", len(signals))
    hours = age.total_seconds() / 3600
    cols[1].metric(
        "Last signal", f"{hours:,.1f} h ago" if hours < 48 else f"{hours / 24:,.0f} d ago"
    )
    cols[2].metric("Last value", f"{signals['signal_value'].iloc[-1]:.4g}")
    if bars.empty:
        st.info(
            "This run stored no bars for this symbol, so there is nothing to measure signals "
            f"against. {common.missing_bars(selected.run['mode'])}"
        )
        return
    cols[3].metric(
        "Since last signal",
        _pct(queries.latest_signal_return(signals["ts"], bars, direction, price_col)),
        help="Direction-adjusted return from the latest signal's reference price to the "
        "last close.",
    )

    with st.spinner("Measuring forward outcomes"):
        outcomes = _outcomes(run_id, signal_type, symbol, int(n), direction, price_col)
    cols = st.columns(4)
    cols[0].metric(f"Mean return T+{n}", _pct(outcomes["forward_return"].mean()))
    cols[1].metric(
        f"Edge ratio T+{n}",
        f"{queries.edge_ratio(outcomes):.2f}" if not outcomes.empty else "–",
        help="Mean favourable excursion over mean adverse excursion within the horizon.",
    )
    cols[2].metric("Signals measured", len(outcomes))

    shown = common.recent(bars)
    in_view = signals[signals["ts"] >= shown["ts"].min()]
    st.plotly_chart(
        charts.candles_with_markers(
            shown, queries.signal_markers(in_view, bars, f"{signal_type.title()} signal")
        )
    )
    st.caption("Markers sit at the signal bar's close; returns start from the next bar.")

    if outcomes.empty:
        st.info(f"No signal has {n} bars after it yet.")
        return
    stats = queries.rolling_signal_stats(outcomes, int(k))
    st.subheader(f"Forward outcomes at T+{n}")
    left, right = st.columns(2)
    left.plotly_chart(charts.lines(stats, {"cumulative_return": "Cumulative return"}, percent=True))
    left.caption("Sum of each signal's forward return, in signal order.")
    right.plotly_chart(
        charts.lines(stats, {"rolling_mean_return": f"Rolling {k} mean return"}, percent=True)
    )
    st.plotly_chart(charts.lines(stats, {"rolling_edge_ratio": f"Rolling {k} edge ratio"}))


# Recomputed only when a reader changes a control; the page does not auto-refresh.
@st.cache_data(ttl=300, show_spinner=False)
def _outcomes(
    run_id: str, signal_type: str, symbol: str, n: int, direction: str, price_col: str
) -> pd.DataFrame:
    rows = queries.HISTORY_ROWS
    signals = queries.oldest_first(db.query(queries.SIGNALS, (run_id, signal_type, symbol, rows)))
    bars = queries.oldest_first(db.query(queries.RUN_BARS, (run_id, symbol, rows)))
    return queries.signal_outcomes_at(signals["ts"], bars, n, direction, price_col)
