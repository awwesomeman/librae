"""A run's raw signals against the bars it stored, with their forward outcomes."""

from __future__ import annotations

import math

import pandas as pd
import streamlit as st

from librae.console import charts, common, db, queries, ui


def _pct(value: float | None) -> str:
    return "–" if value is None or math.isnan(value) else f"{value:.2%}"


def render(selected: common.Selection) -> None:
    _body(selected)


def _body(selected: common.Selection) -> None:
    run_id = selected.run["run_id"]
    controls = st.columns(4)
    signal_type = (
        controls[0].segmented_control(
            "Signal",
            ["entry", "exit"],
            default="entry",
            key="librae_signal_type",
            format_func=str.title,
        )
        or "entry"
    )
    direction = (
        controls[1].segmented_control(
            "Expected Direction",
            ["long", "short"],
            default="long",
            key="librae_signal_direction",
            format_func=str.title,
            help="The price move the signal predicts; independent of entry or exit.",
        )
        or "long"
    )
    price_col = controls[2].selectbox(
        "Reference Price",
        ["close", "open", "high", "low"],
        key="librae_signal_price",
        format_func=str.title,
        help="Field of the first bar after the signal that returns are measured from.",
    )
    symbols = db.query(queries.SIGNAL_SYMBOLS, (run_id, signal_type))["symbol"].tolist()
    if not symbols:
        st.info(f"This run recorded no {signal_type} signals.")
        return
    symbol = controls[3].selectbox("Symbol", symbols, key="librae_signal_symbol")
    _snapshot(run_id, signal_type, symbol, direction, price_col)
    _analysis(selected, signal_type, symbol, direction, price_col)


@st.fragment(run_every=common.REFRESH_SECONDS)
def _snapshot(run_id: str, signal_type: str, symbol: str, direction: str, price_col: str) -> None:
    summary = db.query(queries.SIGNAL_SUMMARY, (run_id, signal_type, symbol, queries.HISTORY_ROWS))
    if summary.empty or not summary.iloc[0]["signal_count"]:
        common.refresh_toolbar(None, "signal", what="Latest stored price")
        st.info("No signals recorded for this selection.")
        return
    last = summary.iloc[0]
    reference = db.query(queries.SIGNAL_REFERENCE, (run_id, symbol, last["last_ts"]))
    marks = db.query(queries.LATEST_MARKS, (run_id,))
    mark = marks.loc[marks["symbol"] == symbol]
    common.refresh_toolbar(
        mark["ts"].max() if not mark.empty else None,
        "signal",
        what="Latest stored price",
    )
    ui.section_header("Latest Signals")
    value = None
    if not reference.empty and not mark.empty and mark.iloc[0]["ts"] >= reference.iloc[0]["ts"]:
        value = queries.signal_return(
            reference.iloc[0][price_col], mark.iloc[0]["close"], direction
        )
    cols = st.columns(4)
    cols[0].metric(
        "Signals",
        f"{int(last['signal_count']):,}",
        help=f"Count for this symbol and signal type, up to the latest {queries.HISTORY_ROWS:,} signals.",
    )
    cols[1].metric("Last Signal", f"{queries.format_age(common.now() - last['last_ts'])} ago")
    cols[2].metric(
        "Last Value", f"{last['last_value']:.4g}" if pd.notna(last["last_value"]) else "—"
    )
    cols[3].metric(
        "Since Last Signal",
        _pct(value),
        help="Direction-adjusted return from the first bar after the signal to the latest "
        "stored close, using Reference Price. Unavailable until that next bar is stored.",
    )


@st.fragment(run_every=common.HISTORY_REFRESH_SECONDS)
def _analysis(
    selected: common.Selection, signal_type: str, symbol: str, direction: str, price_col: str
) -> None:
    ui.section_header("Signal Analysis")
    run_id = selected.run["run_id"]
    horizon, window = st.columns(2)
    n = horizon.number_input("Horizon (Bars)", min_value=1, value=24, key="librae_signal_n")
    k = window.number_input(
        "Rolling Window (Signals)", min_value=2, value=50, key="librae_signal_k"
    )

    signals = common.history(queries.SIGNALS, (run_id, signal_type, symbol))
    bars = common.history(queries.RUN_BARS, (run_id, symbol))
    if bars.empty:
        st.info(
            "This run stored no bars for this symbol, so there is nothing to measure signals "
            f"against. {common.missing_bars(selected.run['mode'])}"
        )
        return
    shown = common.recent(bars)
    in_view = signals[signals["ts"] >= shown["ts"].min()]
    st.plotly_chart(
        charts.candles_with_markers(
            shown, queries.signal_markers(in_view, bars, f"{signal_type.title()} signal")
        ),
        key=f"signal_price_{run_id}_{signal_type}_{symbol}",
    )
    st.caption("Markers sit at the signal bar's close; returns start from the next bar.")

    with st.spinner("Measuring forward outcomes"):
        outcomes = _outcomes(signals["ts"], bars, int(n), direction, price_col)
    ui.section_header("Forward Outcomes", f"Measured over {n} bars")
    cols = st.columns(3)
    cols[0].metric(f"Mean Return T+{n}", _pct(outcomes["forward_return"].mean()))
    cols[1].metric(
        f"Edge Ratio T+{n}",
        f"{queries.edge_ratio(outcomes):.2f}" if not outcomes.empty else "–",
        help="Mean favourable excursion over mean adverse excursion within the horizon.",
    )
    cols[2].metric("Signals Measured", len(outcomes))

    if outcomes.empty:
        st.info(f"No signal has {n} bars after it yet.")
        return
    stats = queries.rolling_signal_stats(outcomes, int(k))
    ui.section_header("Outcome Trends")
    left, right = st.columns(2)
    chart_key = f"signal_{run_id}_{signal_type}_{symbol}_{direction}_{price_col}_{n}_{k}"
    left.subheader(
        "Cumulative Return",
        help="Sum of each signal's forward return. Each signal is counted independently.",
    )
    left.plotly_chart(
        charts.lines(stats, {"cumulative_return": "Cumulative return"}, percent=True),
        key=f"{chart_key}_cumulative",
    )
    right.subheader("Rolling Mean Return")
    right.plotly_chart(
        charts.lines(stats, {"rolling_mean_return": f"Rolling {k} mean return"}, percent=True),
        key=f"{chart_key}_mean",
    )
    st.subheader("Rolling Edge Ratio")
    st.plotly_chart(
        charts.lines(stats, {"rolling_edge_ratio": f"Rolling {k} edge ratio"}),
        key=f"{chart_key}_edge",
    )


@st.cache_data(ttl=common.HISTORY_REFRESH_SECONDS, show_spinner=False)
def _outcomes(
    signal_ts: pd.Series, bars: pd.DataFrame, n: int, direction: str, price_col: str
) -> pd.DataFrame:
    return queries.signal_outcomes_at(signal_ts, bars, n, direction, price_col)
