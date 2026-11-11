"""A run's runtime events: restarts, skipped decisions, halts and group failures."""

from __future__ import annotations

import streamlit as st

from librae.console import common, db, queries, ui


def render(selected: common.Selection) -> None:
    _events(selected.run["run_id"])


@st.fragment(run_every=common.REFRESH_SECONDS)
def _events(run_id: str) -> None:
    events = db.query(queries.RUNTIME_EVENTS, (run_id, queries.RECENT_EVENTS))
    if events.empty:
        st.info("No runtime events: the run has not restarted, skipped a decision or halted.")
        return
    ui.section_header("Event Log", f"Run-wide · Latest {len(events):,} events")
    kinds = sorted(events["event_type"].unique())
    chosen = st.multiselect("Event Types", kinds, default=kinds)
    shown = events[events["event_type"].isin(chosen)]
    st.dataframe(
        shown,
        column_config={
            "ts": st.column_config.DatetimeColumn("Time"),
            "event_type": st.column_config.TextColumn("Event"),
            "symbol": st.column_config.TextColumn("Symbol"),
            "reason": st.column_config.TextColumn("Reason"),
            "detail": st.column_config.TextColumn("Detail"),
        },
        column_order=["ts", "event_type", "symbol", "reason", "detail"],
        hide_index=True,
    )
    common.as_of(events["ts"].max())
