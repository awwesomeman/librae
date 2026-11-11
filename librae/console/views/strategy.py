"""One strategy run, with account results and run-wide analysis in one place."""

from __future__ import annotations

import streamlit as st

from librae.console import common, db, queries, ui
from librae.console.views import events, performance, positions

_SECTIONS = {
    "Performance": performance.render,
    "Positions": positions.render,
    "Runtime Events": events.render,
}


def render() -> None:
    selected = common.selection()
    if selected is None:
        ui.page_header("Strategy")
        return
    run = selected.run
    ui.page_header(
        run["strategy_name"],
        "Review performance, positions and runtime events for one strategy run.",
        section="Strategy",
    )
    common.status_panel(run["run_id"], f"Run · {run['run_at']:%Y-%m-%d %H:%M:%S}")

    signal_runs = db.query(queries.SIGNAL_RUNS, (queries.RECENT_RUNS,))
    if run["run_id"] in signal_runs["run_id"].values:
        st.button(
            "View Signals",
            type="tertiary",
            icon=":material/arrow_outward:",
            on_click=common.request_signal,
            args=(run["run_id"],),
        )
    common._keep_valid(common.SECTION_KEY, list(_SECTIONS))
    section = st.radio(
        "Strategy Sections",
        list(_SECTIONS),
        key=common.SECTION_KEY,
        horizontal=True,
        label_visibility="collapsed",
    )
    if section in ("Performance", "Positions"):
        selected = common.account_selector(selected)
    _SECTIONS[section](selected)
