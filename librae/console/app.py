"""Console entry point; ``streamlit run`` this file or use ``python -m librae.console``."""

from __future__ import annotations

import os

import streamlit as st

from librae.console import common
from librae.console.db import DSN_ENV
from librae.console.views import events, performance, positions, runs, signals

st.set_page_config(page_title="Librae console", layout="wide")

if not os.environ.get(DSN_ENV):
    st.error(
        f"Set `{DSN_ENV}` to a PostgreSQL connection string for a read-only role, "
        "then restart the console."
    )
    st.stop()

runs_page = st.Page(runs.render, title="Runs", url_path="runs")


def _home() -> None:
    # The default page is served at the root URL only, so Runs is not the
    # default: it keeps its own /runs link, and the root forwards to it.
    st.switch_page(runs_page)


page = st.navigation(
    [
        st.Page(_home, title="Home", url_path="home", default=True, visibility="hidden"),
        runs_page,
        st.Page(performance.render, title="Performance", url_path="performance"),
        st.Page(positions.render, title="Positions", url_path="positions"),
        st.Page(signals.render, title="Signals", url_path="signals"),
        st.Page(events.render, title="Runtime events", url_path="events"),
    ]
)
common.sidebar()
page.run()
