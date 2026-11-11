"""Console entry point; ``streamlit run`` this file or use ``python -m librae.console``."""

from __future__ import annotations

import os

import streamlit as st

from librae.console import common, ui
from librae.console.db import DSN_ENV
from librae.console.views import runs, signal, strategy

st.set_page_config(page_title="Librae Console", layout="wide", initial_sidebar_state="expanded")
ui.apply_style()
with st.sidebar:
    st.html(
        '<div class="brand"><div class="brand-mark">LB</div><div>'
        '<div class="brand-name">librae</div>'
        '<div class="brand-caption">Strategy Console</div></div></div>'
    )

if not os.environ.get(DSN_ENV):
    st.error(
        f"Set `{DSN_ENV}` to a PostgreSQL connection string for a read-only role, "
        "then restart the console."
    )
    st.stop()

overview_page = st.Page(runs.render, title="Overview", url_path="overview")


def _home() -> None:
    # Keep a dedicated /overview route as well as the root entry point.
    st.switch_page(overview_page)


pages = [
    overview_page,
    st.Page(strategy.render, title="Strategy", url_path="strategy"),
    st.Page(signal.render, title="Signal", url_path="signal"),
]
page = st.navigation(
    [st.Page(_home, title="Home", url_path="home", default=True, visibility="hidden"), *pages],
    position="hidden",
)
with st.sidebar:
    for item, icon in zip(
        pages,
        ["dashboard", "monitoring", "sensors"],
        strict=True,
    ):
        st.page_link(item, icon=f":material/{icon}:")
open_run = common.run_requested()
open_signal = common.signal_requested()
if open_run or (page.url_path == "strategy" and not open_signal):
    common.sidebar(page="strategy")
elif open_signal or page.url_path == "signal":
    common.sidebar(page="signal")
else:
    common.sidebar(page="overview")
if open_run:
    st.switch_page(pages[1])
if open_signal:
    st.switch_page(pages[2])
page.run()
