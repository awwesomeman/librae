"""Every run's latest account state: what is running, and where each account stands."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from librae.console import common, db, queries, ui

_SHOWN_KEY = "librae_runs_shown"
_ACCOUNTS_KEY = "librae_runs_accounts_shown"
_CLICK_KEY = "librae_strategy_click"

_COLUMNS = {
    "strategy_name": st.column_config.TextColumn("Strategy"),
    "mode": st.column_config.TextColumn("Mode"),
    "status": st.column_config.TextColumn(
        "Status",
        help="running: heartbeat within three poll cycles; stale: heartbeats missed; "
        "halted: the checkpoint says so; not polling: a backtest or a run without a heartbeat.",
    ),
    "freshness": st.column_config.TextColumn(
        "Data Freshness",
        help="Latest account equity: Current = within two bars plus three poll cycles; "
        "Delayed = beyond that limit; Historical = backtest, no live freshness check; "
        "No Data = no equity recorded; Not Monitored = no polling heartbeat. "
        "Market closures can also explain delayed data.",
    ),
    "account_id": st.column_config.TextColumn("Account"),
    "currency": st.column_config.TextColumn("Currency"),
    "equity": st.column_config.NumberColumn("Equity", format="%.2f"),
    "gross_exposure": st.column_config.NumberColumn("Gross Exposure", format="percent"),
    "net_exposure": st.column_config.NumberColumn("Net Exposure", format="percent"),
    "concentration": st.column_config.NumberColumn("Concentration", format="percent"),
    "open_positions": st.column_config.NumberColumn("Open Positions"),
    "last_equity_at": st.column_config.DatetimeColumn("Last Equity"),
    "last_heartbeat_at": st.column_config.DatetimeColumn("Heartbeat"),
    "run_id": st.column_config.TextColumn("Run ID"),
}


def render() -> None:
    ui.page_header(
        "Overview",
        "Monitor strategy runs and accounts. Open a strategy to inspect its performance and activity.",
    )
    _overview()


@st.fragment(run_every=common.REFRESH_SECONDS)
def _overview() -> None:
    overview = db.query(queries.ACCOUNT_OVERVIEW, (queries.RECENT_RUNS,))
    if overview.empty:
        st.info("No runs recorded yet. Saved strategy runs and signal analyses appear here.")
        return
    table = queries.with_status(overview, common.now())
    cols = st.columns(4)
    cols[0].metric(
        "Runs",
        table["run_id"].nunique(),
        help=f"The latest {queries.RECENT_RUNS} recorded runs, including runs without account data.",
    )
    cols[1].metric("Running", table.loc[table["status"] == "running", "run_id"].nunique())
    attention = table["status"].isin(["stale", "halted"]) | table["behind"]
    cols[2].metric(
        "Needs Attention",
        table.loc[attention, "run_id"].nunique(),
        help="Runs with a stale heartbeat, a halt flag, or overdue equity data. Market closures can also explain overdue data.",
    )
    cols[3].metric(
        "Accounts",
        table["account_id"].nunique(),
        help="Distinct account IDs among these runs; balances are not combined.",
    )
    ui.section_header("Run Directory", f"Latest {queries.RECENT_RUNS} runs · Open a strategy name")
    if table["behind"].any():
        st.warning(
            "A polling run has overdue equity data: see Data Freshness.", icon=":material/schedule:"
        )
    modes = sorted(table["mode"].unique())
    chosen = st.multiselect("Mode", modes, default=modes, format_func=str.title)
    table = table[table["mode"].isin(chosen)].reset_index(drop=True)
    table["freshness"] = [freshness_label(row) for _, row in table.iterrows()]
    st.session_state[_SHOWN_KEY] = table["run_id"].tolist()
    st.session_state[_ACCOUNTS_KEY] = (
        table["account_id"].where(table["account_id"].notna(), None).tolist()
    )
    st.dataframe(
        table[list(_COLUMNS)].assign(
            status=table["status"].str.title(), mode=table["mode"].str.title()
        ),
        column_config={
            **_COLUMNS,
            "strategy_name": st.column_config.ButtonColumn(
                "Strategy",
                type="tertiary",
                alignment="left",
                width="medium",
                pinned=True,
                help="Open this run and account in Strategy.",
                on_click=_apply_click,
                key=_CLICK_KEY,
            ),
        },
        hide_index=True,
        key="librae_run_directory",
    )
    if common.run_requested():
        st.rerun()  # inside a fragment this reruns the whole app, so the sidebar redraws
    common.as_of(overview["last_equity_at"].max())


def clicked_run(row: int | None, shown: list[str]) -> str | None:
    """Resolve a name click against the exact rows displayed, including after sorting."""
    if row is not None and 0 <= row < len(shown):
        return shown[row]
    return None


def freshness_label(row: pd.Series) -> str:
    """Describe the existing equity freshness flag without treating missing data as current."""
    if pd.isna(row["last_equity_at"]):
        return "No Data"
    if row["mode"] == "backtest":
        return "Historical"
    if row["status"] == "not polling":
        return "Not Monitored"
    return "Delayed" if row["behind"] else "Current"


def _apply_click() -> None:
    """Button clicks are transient; no row selection survives a refresh or filter."""
    click = st.session_state.get(_CLICK_KEY)
    if click is None:
        return
    row = click["row"]
    run_id = clicked_run(row, st.session_state.get(_SHOWN_KEY, []))
    if run_id is not None:
        accounts = st.session_state.get(_ACCOUNTS_KEY, [])
        common.request_run(run_id, accounts[row] if row < len(accounts) else None)
