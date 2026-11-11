"""One run's KPIs, equity curve and drawdown, exposure, and trade returns."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from librae.console import charts, common, db, queries, ui

_RECOMPUTED = (
    "From the run's stored performance summary, as of its last recompute (a sim or live "
    "run recomputes on a reduce, close or funding event; a backtest once, when saved). "
    "Computed from the equity curve up to then, so open positions count as marked at "
    "that time."
)
_CLOSED_TRADES = (
    "From closed trades only, as of the summary's last recompute; legs sharing a "
    "group_id and entry time count as one trade."
)


def _fmt(value: object, spec: str) -> str:
    if value is None or pd.isna(value):
        return "–"
    return format(value, spec)


def render(selected: common.Selection) -> None:
    run = selected.run
    if selected.account_id is None:
        st.info("This run has not written an equity curve yet.")
        return
    _account_now(
        run["run_id"],
        selected.account_id,
        selected.currency,
        run["timeframe"],
        run["poll_seconds"] if run["mode"] != "backtest" else None,
    )
    _history(selected)


@st.fragment(run_every=common.REFRESH_SECONDS)
def _account_now(
    run_id: str,
    account_id: str,
    currency: str | None,
    timeframe: str,
    poll_seconds: float | None,
) -> None:
    """Equity and open positions now; ``poll_seconds`` is None for a run that does not poll."""
    latest = db.query(queries.LATEST_EQUITY, (run_id, account_id))
    positions = db.query(queries.OPEN_POSITIONS, (run_id, account_id))
    marks = db.query(queries.LATEST_MARKS, (run_id,))
    latest_equity = float(latest["equity"].iloc[0]) if not latest.empty else None
    snapshot = queries.position_snapshot(positions, marks, latest_equity)
    unrealized = queries.complete_total(snapshot["mtm_pnl"])
    margin = queries.complete_total(snapshot["margin_locked"])
    denominator = latest_equity if latest_equity and latest_equity > 0 else float("nan")
    unit = f" {currency}" if currency else ""
    latest_ts = latest["ts"].iloc[0] if not latest.empty else None
    common.refresh_toolbar(latest_ts, "performance", what="Newest equity point")
    if poll_seconds is not None and queries.is_behind(
        latest_ts, timeframe, poll_seconds, common.now()
    ):
        st.warning(
            "The latest equity point is older than two bars plus three poll cycles: "
            "the run may have stalled (a market closure also explains it).",
            icon=":material/schedule:",
        )

    cols = st.columns(4)
    cols[0].metric(f"Equity ({currency})" if currency else "Equity", _fmt(latest_equity, ",.2f"))
    cols[1].metric("Open Positions", len(positions))
    cols[2].metric(
        "Unrealized P&L",
        _fmt(unrealized, ",.2f"),
        delta=f"{_fmt(unrealized / denominator, '.1%')} of equity",
        delta_color="off",
        delta_arrow="off",
        help=f"Open-position profit or loss{unit}, marked to stored bars. "
        "Unavailable if any position lacks a mark.",
    )
    cols[3].metric(
        "Margin Locked",
        _fmt(margin / denominator, ".1%"),
        delta=f"{_fmt(margin, ',.2f')}{unit}",
        delta_color="off",
        delta_arrow="off",
        help="Capital locked by open positions, as a share of equity. Zero when flat.",
    )
    if not positions.empty:
        with st.container(horizontal=True, horizontal_alignment="right"):
            breakdown = queries.margin_breakdown(snapshot, latest_equity).sort_values(
                "amount", ascending=False, na_position="last"
            )
            ui.margin_breakdown(
                [
                    (row.margin_mode, _fmt(row.amount, ",.2f"), _fmt(row.share, ".1%"))
                    for row in breakdown.itertuples()
                ],
                currency,
            )
    if snapshot["mtm_pnl"].isna().any() or snapshot["margin_locked"].isna().any():
        st.caption("Some positions lack a price or margin value; incomplete totals show —.")


@st.fragment(run_every=common.HISTORY_REFRESH_SECONDS)
def _history(selected: common.Selection) -> None:
    run_id, account_id = selected.run["run_id"], selected.account_id
    equity = common.history(queries.EQUITY, (run_id, account_id))
    events = common.history(queries.POSITION_EVENTS, (run_id, account_id))
    performance = db.query(queries.PERFORMANCE, (run_id, account_id))
    curve = queries.with_drawdown(equity)

    _account_charts(curve, events, run_id, account_id)
    ui.section_header("Performance Details", "Full Run")
    if performance.empty:
        st.info("No stored performance summary for this account yet.")
    kpi = performance.iloc[0] if not performance.empty else pd.Series(dtype=object)
    worst = curve["drawdown"].min() if not curve.empty else None
    worst_amount = curve["drawdown_amount"].min() if not curve.empty else None
    unit = f" {selected.currency}" if selected.currency else ""
    ui.result_table(
        [
            (
                "Total Return",
                _fmt(kpi.get("total_return"), ".2%"),
                f"{_fmt(kpi.get('net_pnl'), ',.2f')}{unit}" if pd.notna(kpi.get("net_pnl")) else "",
                _RECOMPUTED + " The amount is net P&L: final equity minus initial cash.",
            ),
            (
                "Max Equity Drawdown",
                _fmt(worst, ".2%"),
                f"{_fmt(worst_amount, ',.2f')}{unit}" if pd.notna(worst_amount) else "",
                "Largest fall from a previous equity peak over the equity rows loaded "
                f"(the latest {queries.HISTORY_ROWS:,} at most), with open positions as "
                "marked. The worst percentage and amount can occur at different times.",
            ),
            (
                "Max Drawdown at Last Recompute",
                _fmt(kpi.get("max_drawdown"), ".2%"),
                "",
                _RECOMPUTED,
            ),
            ("Trades", _fmt(kpi.get("trades"), "d"), "", _CLOSED_TRADES),
            (
                "Period Sharpe",
                _fmt(kpi.get("period_sharpe"), ".2f"),
                "",
                "Sharpe of the equity curve's period returns; not annualized. " + _RECOMPUTED,
            ),
            (
                "Period Sortino",
                _fmt(kpi.get("period_sortino"), ".2f"),
                "",
                "Sortino of the equity curve's period returns; measures downside variability. "
                "Not annualized. " + _RECOMPUTED,
            ),
            ("Win Rate", _fmt(kpi.get("win_rate"), ".1%"), "", _CLOSED_TRADES),
            (
                "Profit Factor",
                _fmt(kpi.get("profit_factor"), ".2f"),
                "",
                "Gross profits over gross losses of closed trades. " + _CLOSED_TRADES,
            ),
        ]
    )


def _account_charts(
    equity: pd.DataFrame, events: pd.DataFrame, run_id: str, account_id: str | None
) -> None:
    """``equity`` carries the drawdown columns ``queries.with_drawdown`` adds."""
    if equity.empty:
        st.info("No equity points for this account.")
        return
    ui.section_header("Account Equity")
    st.plotly_chart(
        charts.equity_and_drawdown(equity),
        key=f"equity_{run_id}_{account_id}",
    )
    if len(equity) == queries.HISTORY_ROWS:
        st.caption(
            "Drawdown, here and as Max Equity Drawdown below, is measured only within "
            "the rows shown, from their own peak."
        )

    left, right = st.columns(2)
    with left:
        st.subheader("Exposure")
        st.plotly_chart(
            charts.lines(
                equity,
                {"gross_exposure": "Gross", "net_exposure": "Net", "concentration": "Largest"},
                percent=True,
            ),
            key=f"exposure_{run_id}_{account_id}",
        )
        st.caption("Notional as a share of equity; Largest is the biggest single position.")
    with right:
        st.subheader("Trade Returns")
        returns = queries.trade_returns(events)
        if returns.empty:
            st.info("No closed trades yet.")
        else:
            st.plotly_chart(
                charts.histogram(returns, "Net return on entry notional, per trade"),
                key=f"trade_returns_{run_id}_{account_id}",
            )
            st.caption(
                "One value per exit; legs sharing a group_id and entry time count as one "
                "trade, as in Performance Details. Financing is not included."
            )
