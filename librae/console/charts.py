"""Plotly figures for the console pages."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

_MARGIN = {"l": 10, "r": 10, "t": 30, "b": 10}


def equity_and_drawdown(equity: pd.DataFrame) -> go.Figure:
    """Equity on top, drawdown below on its own axis, sharing the time axis."""
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.7, 0.3],
        vertical_spacing=0.04,
        subplot_titles=("Equity", "Drawdown"),
    )
    fig.add_trace(
        go.Scatter(x=equity["ts"], y=equity["equity"], name="Equity", line={"width": 2}),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=equity["ts"],
            y=equity["drawdown"],
            name="Drawdown",
            fill="tozeroy",
            line={"width": 1},
        ),
        row=2,
        col=1,
    )
    fig.update_yaxes(tickformat=".1%", row=2, col=1)
    fig.update_layout(height=480, showlegend=False, margin=_MARGIN, hovermode="x unified")
    return fig


def lines(frame: pd.DataFrame, columns: dict[str, str], *, percent: bool = False) -> go.Figure:
    """One line per ``{column: label}`` against ``frame["ts"]``."""
    fig = go.Figure()
    for column, label in columns.items():
        fig.add_trace(go.Scatter(x=frame["ts"], y=frame[column], name=label, line={"width": 2}))
    fig.update_layout(
        height=320,
        margin=_MARGIN,
        hovermode="x unified",
        showlegend=len(columns) > 1,
        legend={"orientation": "h", "y": 1.1},
    )
    if percent:
        fig.update_yaxes(tickformat=".1%")
    return fig


def histogram(values: pd.Series, label: str) -> go.Figure:
    fig = go.Figure(go.Histogram(x=values, name=label, nbinsx=40))
    fig.update_layout(height=320, margin=_MARGIN, bargap=0.05)
    fig.update_xaxes(title_text=label, tickformat=".1%")
    return fig


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
        height=520,
        margin=_MARGIN,
        xaxis_rangeslider_visible=False,
        legend={"orientation": "h", "y": 1.05},
    )
    return fig
