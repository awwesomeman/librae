"""Presentation helpers for the read-only console; no queries or trading logic."""

from __future__ import annotations

from html import escape
from pathlib import Path

import streamlit as st

THEME_DEFAULTS = {
    "STREAMLIT_THEME_BASE": "light",
    "STREAMLIT_THEME_PRIMARY_COLOR": "#087f8c",
    "STREAMLIT_THEME_BACKGROUND_COLOR": "#f7f9fb",
    "STREAMLIT_THEME_SECONDARY_BACKGROUND_COLOR": "#ffffff",
    "STREAMLIT_THEME_TEXT_COLOR": "#172b35",
    "STREAMLIT_THEME_BORDER_COLOR": "#e3e9ed",
    "STREAMLIT_THEME_FONT": '"Segoe UI", sans-serif',
    "STREAMLIT_THEME_BASE_FONT_SIZE": "14",
    "STREAMLIT_THEME_SHOW_WIDGET_BORDER": "true",
    "STREAMLIT_THEME_SIDEBAR_BACKGROUND_COLOR": "#152d38",
    "STREAMLIT_THEME_SIDEBAR_SECONDARY_BACKGROUND_COLOR": "#23424e",
    "STREAMLIT_THEME_SIDEBAR_TEXT_COLOR": "#dce8ee",
    "STREAMLIT_THEME_SIDEBAR_PRIMARY_COLOR": "#76c9d1",
    "STREAMLIT_THEME_SIDEBAR_BORDER_COLOR": "#314852",
}


def apply_style() -> None:
    st.html(Path(__file__).with_name("style.css"))


def page_header(title: str, description: str = "", *, section: str | None = None) -> None:
    st.html(
        '<div class="page-topline"><span>Console <span class="breadcrumb-slash">/</span> '
        f"{escape(section or title)}</span><span>Read-only · UTC</span></div>"
    )
    st.title(title)
    if description:
        st.html(f'<p class="page-description">{escape(description)}</p>')


def section_header(title: str, note: str = "") -> None:
    st.html(
        f'<div class="section-heading"><h2>{escape(title)}</h2><span>{escape(note)}</span></div>'
    )


def hint(label: str, explanation: str) -> str:
    """Expose a short definition on hover, keyboard focus and touch."""
    return (
        '<span class="detail-hint"><button type="button" '
        f'aria-label="{escape(label + ". " + explanation)}">{escape(label)}</button>'
        f'<span class="detail-tooltip" role="tooltip">{escape(explanation)}</span></span>'
    )


def result_table(rows: list[tuple[str, str, str, str]]) -> None:
    """Keep preformatted results in paired metric/value tables that stack on narrow screens."""
    midpoint = (len(rows) + 1) // 2
    tables = []
    for group in (rows[:midpoint], rows[midpoint:]):
        body = "".join(
            '<tr><th scope="row">'
            + hint(label, explanation)
            + '</th><td><div class="result-value">'
            + f'<span class="result-primary">{escape(value)}</span>'
            + (f'<span class="result-secondary">{escape(amount)}</span>' if amount else "")
            + "</div></td></tr>"
            for label, value, amount, explanation in group
        )
        tables.append(
            '<table class="result-table" aria-label="Performance Details">'
            '<thead><tr><th scope="col">Metric</th><th scope="col">Value</th></tr></thead>'
            f"<tbody>{body}</tbody></table>"
        )
    st.html('<div class="results-grid">' + "".join(tables) + "</div>")


def margin_breakdown(rows: list[tuple[str, str, str]], currency: str | None) -> None:
    """Reveal margin amounts and equity shares on hover, focus or touch."""
    meanings = {
        "unlevered": "Full notional is locked; no leverage.",
        "fixed": "Margin is set by the exchange or regulator.",
        "dynamic": "Margin is based on trader-selected leverage.",
        "Unknown": "No margin mode is recorded for these positions.",
    }
    body = "".join(
        '<tr><th scope="row">'
        + f'<span title="{escape(meanings.get(mode, "Margin mode recorded for these positions."))}">'
        + escape(mode.title())
        + "</span>"
        + f"</th><td>{escape(amount)}</td><td>{escape(share)}</td></tr>"
        for mode, amount, share in rows
    )
    unit = f" ({escape(currency)})" if currency else ""
    st.html(
        '<div class="margin-hint"><button type="button" class="margin-trigger" '
        'aria-describedby="librae-margin-details">Margin Breakdown</button>'
        '<div class="margin-breakdown" id="librae-margin-details" role="tooltip">'
        "<table><caption>Margin Breakdown</caption>"
        f'<thead><tr><th scope="col">Mode</th><th scope="col">Locked{unit}</th>'
        '<th scope="col">Of Equity</th></tr></thead>'
        f"<tbody>{body}</tbody></table></div></div>",
        width="content",
    )
