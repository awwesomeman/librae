"""Open positions marked to market, the fills behind them, and price with fills."""

from __future__ import annotations

import streamlit as st

from librae.console import charts, common, db, queries, ui

_SNAPSHOT_COLUMNS = {
    "symbol": st.column_config.TextColumn("Symbol"),
    "side": st.column_config.TextColumn("Side"),
    "weight": st.column_config.NumberColumn(
        "Weight", format="percent", help="Signed notional as a share of equity."
    ),
    "remaining_quantity": st.column_config.NumberColumn("Quantity", format="%.4f"),
    "entry_price": st.column_config.NumberColumn("Entry price", format="%.4f"),
    "market_price": st.column_config.NumberColumn(
        "Mark", format="%.4f", help="Close of the run's latest stored bar."
    ),
    "mtm_pnl": st.column_config.NumberColumn("MTM P&L", format="%.2f"),
    "mtm_return": st.column_config.NumberColumn("MTM return", format="percent"),
    "margin_locked": st.column_config.NumberColumn("Margin locked", format="%.2f"),
    "leverage": st.column_config.NumberColumn("Leverage", format="%.2f"),
    "margin_mode": st.column_config.TextColumn("Margin mode"),
    "liquidation_price": st.column_config.NumberColumn("Liquidation", format="%.4f"),
    "liquidation_buffer": st.column_config.NumberColumn(
        "Liquidation buffer", format="percent", help="Distance from mark to liquidation."
    ),
    "entry_at": st.column_config.DatetimeColumn("Entered"),
}

_EVENT_COLUMNS = {
    "ts": st.column_config.DatetimeColumn("Time"),
    "symbol": st.column_config.TextColumn("Symbol"),
    "event_type": st.column_config.TextColumn("Event"),
    "side": st.column_config.TextColumn("Side"),
    "fill_quantity": st.column_config.NumberColumn("Quantity", format="%.4f"),
    "price": st.column_config.NumberColumn("Price", format="%.4f"),
    "cash_flow": st.column_config.NumberColumn("Cash flow", format="%.2f"),
    "realized_pnl": st.column_config.NumberColumn("P&L", format="%.2f"),
    "net_return": st.column_config.NumberColumn(
        "Return %", format="%.2f", help="Net P&L over entry notional, in percent."
    ),
    "margin_roi": st.column_config.NumberColumn(
        "Margin return %", format="%.2f", help="Net P&L over the margin it used, in percent."
    ),
    "entry_price": st.column_config.NumberColumn("Avg entry", format="%.4f"),
    "remaining_quantity": st.column_config.NumberColumn("Position after", format="%.4f"),
    "cost": st.column_config.NumberColumn("Cost", format="%.2f"),
    "margin_mode": st.column_config.TextColumn("Margin mode"),
    "group_id": st.column_config.TextColumn("Group"),
    "periods_held": st.column_config.NumberColumn("Periods"),
    "reason": st.column_config.TextColumn("Reason"),
}


def render(selected: common.Selection) -> None:
    if selected.account_id is None:
        st.info("This run has not written an equity curve yet.")
        return
    _open_positions(selected.run["run_id"], selected.account_id, selected.run["mode"])
    _fills(selected)


@st.fragment(run_every=common.REFRESH_SECONDS)
def _open_positions(run_id: str, account_id: str, mode: str) -> None:
    latest = db.query(queries.LATEST_EQUITY, (run_id, account_id))
    marks = db.query(queries.LATEST_MARKS, (run_id,))
    snapshot = queries.position_snapshot(
        db.query(queries.OPEN_POSITIONS, (run_id, account_id)),
        marks,
        float(latest["equity"].iloc[0]) if not latest.empty else None,
    )
    marked = marks.loc[marks["symbol"].isin(snapshot["symbol"]), "ts"]
    common.refresh_toolbar(
        marked.max() if not marked.empty else latest["ts"].max(),
        "positions",
        what="Latest position mark or equity point",
    )
    ui.section_header("Open Positions", "Marked to the latest stored bar")
    if snapshot.empty:
        st.info("Flat: no open position.")
    else:
        st.dataframe(snapshot, column_config=_SNAPSHOT_COLUMNS, hide_index=True)
        if snapshot["market_price"].isna().any():
            st.caption(f"A position without a mark has no stored bar. {common.missing_bars(mode)}")


@st.fragment(run_every=common.HISTORY_REFRESH_SECONDS)
def _fills(selected: common.Selection) -> None:
    ui.section_header("Price And Fills")
    run_id, account_id = selected.run["run_id"], selected.account_id
    events = common.history(queries.POSITION_EVENTS, (run_id, account_id))

    symbols = db.query(queries.BAR_SYMBOLS, (run_id,))["symbol"].tolist()
    if not symbols:
        st.info(f"This run stored no bars. {common.missing_bars(selected.run['mode'])}")
    else:
        symbol = st.selectbox("Symbol", symbols, key="librae_positions_symbol")
        bars = common.recent(common.history(queries.RUN_BARS, (run_id, symbol)))
        fills = events[events["symbol"] == symbol] if not events.empty else events
        markers = queries.fill_markers(fills)
        markers = markers[markers["ts"] >= bars["ts"].min()] if not bars.empty else markers
        st.plotly_chart(
            charts.candles_with_markers(bars, markers), key=f"fills_{run_id}_{account_id}_{symbol}"
        )

    ui.section_header("Position Events")
    if events.empty:
        st.info("No fills recorded for this account.")
        return
    table = events.assign(cost=events["commission"] + events["slippage"] + events["tax"])
    symbols_filter = st.multiselect("Symbols", sorted(table["symbol"].dropna().unique()))
    if symbols_filter:
        table = table[table["symbol"].isin(symbols_filter)]
    st.dataframe(
        table.sort_values("ts", ascending=False)[list(_EVENT_COLUMNS)],
        column_config=_EVENT_COLUMNS,
        hide_index=True,
    )
