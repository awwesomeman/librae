"""Signal analysis entry point, independent of the account and strategy view selection."""

from __future__ import annotations

from html import escape

import streamlit as st

from librae.console import common, ui
from librae.console.views import signals


def render() -> None:
    ui.page_header(
        "Signal",
        "Measure the price moves after a signal, independently of account performance and trading costs.",
    )
    selected = common.signal_selection()
    if selected is None:
        return
    run = selected.run
    st.html(
        '<div class="run-context"><span class="run-name">'
        + ui.hint(run["strategy_name"], f"Run ID: {run['run_id']}")
        + "</span>"
        + f"<span>{escape(str(run['mode']).title())} · {escape(str(run['timeframe']))}</span>"
        + f"<span>Run · {run['run_at']:%Y-%m-%d %H:%M:%S} UTC</span></div>"
    )
    signals.render(selected)
