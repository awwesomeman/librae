"""Every run's latest account state: what is running, and where each account stands."""

from __future__ import annotations

import streamlit as st

from librae.console import common, db, queries

_SHOWN_KEY = "librae_runs_shown"
_VERSION_KEY = "librae_runs_table_version"

_COLUMNS = {
    "strategy_name": st.column_config.TextColumn("Strategy"),
    "mode": st.column_config.TextColumn("Mode"),
    "status": st.column_config.TextColumn(
        "Status",
        help="running: heartbeat within three poll cycles; stale: heartbeats missed; "
        "halted: the checkpoint says so; not polling: a backtest or a run without a heartbeat.",
    ),
    "behind": st.column_config.CheckboxColumn(
        "Behind",
        help="The latest equity point is older than two bars plus three poll cycles.",
    ),
    "account_id": st.column_config.TextColumn("Account"),
    "currency": st.column_config.TextColumn("Currency"),
    "equity": st.column_config.NumberColumn("Equity", format="%.2f"),
    "gross_exposure": st.column_config.NumberColumn("Gross exposure", format="percent"),
    "net_exposure": st.column_config.NumberColumn("Net exposure", format="percent"),
    "concentration": st.column_config.NumberColumn("Concentration", format="percent"),
    "open_positions": st.column_config.NumberColumn("Open positions"),
    "last_equity_at": st.column_config.DatetimeColumn("Last equity"),
    "last_heartbeat_at": st.column_config.DatetimeColumn("Heartbeat"),
    "run_id": st.column_config.TextColumn("Run ID"),
}


def render() -> None:
    st.header("Runs")
    st.caption(
        "The latest equity snapshot of every run and account. "
        "Select a row to open that run on the other pages."
    )
    _overview()


@st.fragment(run_every=common.REFRESH_SECONDS)
def _overview() -> None:
    overview = db.query(queries.ACCOUNT_OVERVIEW, (queries.RECENT_RUNS,))
    if overview.empty:
        st.info("No run has written an equity curve yet.")
        return
    table = queries.with_status(overview, common.now())
    common.as_of(table["last_equity_at"].max())
    if table["behind"].any():
        st.warning("A polling run is behind: see the Behind column.", icon=":material/schedule:")
    modes = sorted(table["mode"].unique())
    chosen = st.multiselect("Mode", modes, default=modes)
    table = table[table["mode"].isin(chosen)].reset_index(drop=True)
    st.session_state[_SHOWN_KEY] = table["run_id"].tolist()
    st.dataframe(
        table[list(_COLUMNS)],
        column_config=_COLUMNS,
        hide_index=True,
        key=_table_key(),
        on_select=_apply_click,
        selection_mode="single-row",
    )
    if common.run_requested():
        st.rerun()  # inside a fragment this reruns the whole app, so the sidebar redraws


def clicked_run(rows: list[int], shown: list[str]) -> str | None:
    """The run on the clicked row of the table as it was drawn, if that row still exists."""
    if rows and 0 <= rows[0] < len(shown):
        return shown[rows[0]]
    return None


def _table_key() -> str:
    return f"librae_runs_table_{st.session_state.get(_VERSION_KEY, 0)}"


def _apply_click() -> None:
    """Turn a click into a run choice, then drop the selection.

    Streamlit keeps a dataframe's selection as row positions, which point at
    another run, or past the end, once a refresh or filter changes the rows.
    The click is resolved against the rows that were on screen, and the
    table moves to a fresh key so no position outlives the click.
    """
    run_id = clicked_run(
        st.session_state[_table_key()].selection.rows, st.session_state.get(_SHOWN_KEY, [])
    )
    st.session_state[_VERSION_KEY] = st.session_state.get(_VERSION_KEY, 0) + 1
    if run_id is not None:
        common.request_run(run_id)
