"""Plotly figures for the console pages."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

_MARGIN = {"l": 48, "r": 24, "t": 48, "b": 40}


def _style(fig: go.Figure) -> go.Figure:
    """Keep chart typography and colors consistent with the surrounding console."""
    fig.update_layout(
        template="plotly_white",
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        font={"family": "Segoe UI, Arial, sans-serif", "size": 12, "color": "#526571"},
        colorway=["#087f8c", "#b17d42", "#6988ab", "#8b7ca4"],
        uirevision="keep-view",
        legend={
            "orientation": "h",
            "xref": "container",
            "x": 0.04,
            "xanchor": "left",
            "yref": "container",
            "y": 0.99,
            "yanchor": "top",
            "font_size": 11,
        },
        hoverlabel={"bgcolor": "#ffffff", "font_color": "#172b35", "bordercolor": "#dbe4e9"},
    )
    fig.update_xaxes(
        gridcolor="#edf1f4", zerolinecolor="#e3e9ed", title_font_size=12, automargin=True
    )
    fig.update_yaxes(
        gridcolor="#edf1f4", zerolinecolor="#e3e9ed", title_font_size=12, automargin=True
    )
    return fig


def equity_and_drawdown(equity: pd.DataFrame) -> go.Figure:
    """Equity on top, drawdown below on its own axis, sharing the time axis."""
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.7, 0.3],
        vertical_spacing=0.14,
        subplot_titles=("Equity", "Drawdown"),
    )
    fig.add_trace(
        go.Scatter(
            x=equity["ts"], y=equity["equity"], name="Equity", line={"width": 2, "color": "#087f8c"}
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=equity["ts"],
            y=equity["drawdown"],
            name="Drawdown",
            fill="tozeroy",
            line={"width": 1, "color": "#b17d42"},
            fillcolor="rgba(177,125,66,0.12)",
        ),
        row=2,
        col=1,
    )
    fig.update_yaxes(tickformat="~%", nticks=3, row=2, col=1)
    fig.update_annotations(font_size=12, x=0, xanchor="left")
    fig.update_layout(
        height=340, showlegend=False, margin={**_MARGIN, "t": 30}, hovermode="x unified"
    )
    return _style(fig)


def lines(frame: pd.DataFrame, columns: dict[str, str], *, percent: bool = False) -> go.Figure:
    """One line per ``{column: label}`` against ``frame["ts"]``."""
    fig = go.Figure()
    for column, label in columns.items():
        fig.add_trace(go.Scatter(x=frame["ts"], y=frame[column], name=label, line={"width": 2}))
    fig.update_layout(
        height=220,
        margin=_MARGIN,
        hovermode="x unified",
        showlegend=len(columns) > 1,
    )
    if percent:
        fig.update_yaxes(tickformat="~%")
    return _style(fig)


def histogram(values: pd.Series, label: str) -> go.Figure:
    fig = go.Figure(go.Histogram(x=values, name=label, nbinsx=40))
    fig.update_layout(height=220, margin=_MARGIN, bargap=0.05)
    fig.update_xaxes(title_text=label, tickformat="~%")
    return _style(fig)


def candles_with_markers(bars: pd.DataFrame, markers: pd.DataFrame) -> go.Figure:
    """Candlesticks with one marker trace per ``markers["label"]``.

    ``markers`` holds ``ts``, ``price`` and ``label``; each label gets its own
    symbol, so entries and exits differ by shape as well as color.
    """
    fig = go.Figure(
        go.Candlestick(
            x=bars["ts"],
            open=bars["open"],
            high=bars["high"],
            low=bars["low"],
            close=bars["close"],
            name="Price",
            increasing_line_color="#4a8065",
            decreasing_line_color="#bc6262",
        )
    )
    shapes = ("triangle-up", "triangle-down", "circle", "x")
    for index, (label, group) in enumerate(markers.groupby("label", sort=True)):
        fig.add_trace(
            go.Scatter(
                x=group["ts"],
                y=group["price"],
                mode="markers",
                name=str(label),
                marker={
                    "symbol": shapes[index % len(shapes)],
                    "size": 10,
                    "line": {"width": 1, "color": "white"},
                },
            )
        )
    fig.update_layout(
        height=340,
        margin={**_MARGIN, "t": 80 if len(fig.data) > 3 else 48},
        xaxis_rangeslider_visible=False,
    )
    return _style(fig)
