"""One run's KPIs, equity curve and drawdown, exposure, and trade returns."""

from __future__ import annotations

import math

import streamlit as st

from librae.console import charts, common, db, queries

_REALIZED = (
    "From strategy_performance, which a sim or live run updates only when a position "
    "is reduced or closed or funding is applied: realized only, so it can lag the "
    "equity curve below."
)


def _fmt(value: object, spec: str) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "–"
    return format(value, spec)


def render() -> None:
    st.header("Performance")
    selected = common.selection()
    if selected is None:
        return
    run = selected.run
    common.status_panel(run["run_id"])
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
    st.subheader("History")
    st.caption("Not auto-refreshed: reload the page to redraw.")
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
    unrealized = snapshot["mtm_pnl"].sum(min_count=1)
    margin = snapshot["margin_locked"].sum(min_count=1)

    cols = st.columns(4)
    cols[0].metric("Equity", _fmt(latest_equity, ",.2f"), help=currency)
    cols[1].metric("Open positions", len(positions))
    cols[2].metric(
        "Unrealized P&L",
        "Flat" if positions.empty else _fmt(float(unrealized), ",.2f"),
        help="Open positions marked to the run's latest stored bar.",
    )
    cols[3].metric(
        "Margin locked",
        _fmt(float(margin) / latest_equity if latest_equity else None, ".1%"),
        help="Margin locked by open positions, as a share of equity.",
    )
    latest_ts = latest["ts"].iloc[0] if not latest.empty else None
    common.as_of(
        latest_ts,
        behind=poll_seconds is not None
        and queries.is_behind(latest_ts, timeframe, poll_seconds, common.now()),
        what="The latest equity point",
    )


def _history(selected: common.Selection) -> None:
    run_id, account_id = selected.run["run_id"], selected.account_id
    equity = common.history(queries.EQUITY, (run_id, account_id))
    events = common.history(queries.POSITION_EVENTS, (run_id, account_id))
    performance = db.query(queries.PERFORMANCE, (run_id, account_id))

    if performance.empty:
        st.info(
            "No strategy_performance row for this account: a backtest writes it when "
            "saved, a sim or live run when it refreshes its performance."
        )
    else:
        kpi = performance.iloc[0]
        st.caption(_REALIZED)
        cols = st.columns(4)
        cols[0].metric("Total return", _fmt(kpi["total_return"], ".2%"), help=_REALIZED)
        cols[0].caption(f"Net P&L {_fmt(kpi['net_pnl'], ',.2f')} {kpi['currency']}")
        cols[1].metric("Max drawdown", _fmt(kpi["max_drawdown"], ".2%"), help=_REALIZED)
        cols[1].caption(f"Trades {_fmt(kpi['trades'], 'd')}")
        cols[2].metric("Period Sharpe", _fmt(kpi["period_sharpe"], ".2f"))
        cols[2].caption(f"Sortino {_fmt(kpi['period_sortino'], '.2f')}")
        cols[3].metric("Win rate", _fmt(kpi["win_rate"], ".1%"))
        cols[3].caption(f"Profit factor {_fmt(kpi['profit_factor'], '.2f')}")

    if equity.empty:
        st.info("No equity points for this account.")
        return
    st.plotly_chart(charts.equity_and_drawdown(queries.with_drawdown(equity)))
    if len(equity) == queries.HISTORY_ROWS:
        st.caption("Drawdown is measured only within the rows shown, from their own peak.")

    left, right = st.columns(2)
    with left:
        st.subheader("Exposure")
        st.plotly_chart(
            charts.lines(
                equity,
                {"gross_exposure": "Gross", "net_exposure": "Net", "concentration": "Largest"},
                percent=True,
            )
        )
        st.caption("Notional as a share of equity; Largest is the biggest single position.")
    with right:
        st.subheader("Trade returns")
        returns = queries.trade_returns(events)
        if returns.empty:
            st.info("No closed trades yet.")
        else:
            st.plotly_chart(charts.histogram(returns, "Net return on entry notional, per trade"))
            st.caption(
                "One value per exit; legs sharing a group_id and entry time count as one "
                "trade, as in the KPIs above. Financing is not included."
            )
