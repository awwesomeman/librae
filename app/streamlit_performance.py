from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from librae.contracts import SCHEMA_VERSION, validate_dataframe_columns, validate_perf_fields, validate_strategy_context


class SchemaValidationError(ValueError):
    """Raised when payload does not match expected canonical schema."""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# TimescaleDB reader (sole data source)
from db.timescale_reader import (
    list_runs as ts_list_runs,
    load_equity_curve as ts_load_equity_curve,
    load_trade_blotter as ts_load_trade_blotter,
    load_performance as ts_load_performance,
    load_strategy_signals as ts_load_signals,
    load_ohlcv as ts_load_ohlcv,
)


APP_TITLE = "Strategy Backtest Analysis"
APP_CAPTION = "UI build: 2026-03-06-1200"

TABLE_HEIGHT_PERF = 260
TABLE_HEIGHT_PARAM = 280
CHART_HEIGHT_PRICE = 300
CHART_HEIGHT_RETURN = 230

PARAM_TABLE_COLUMNS = ["Key", "Value", "Description"]
PARAM_DEFAULT_VALUE = "MISSING"
PARAM_DEFAULT_DESCRIPTION = "Reserved for future extension."

SAMPLE_PREFERRED_ORDER = ["oos", "train", "full", "validation"]

def sample_label(sample: str, periods: dict[str, Any]) -> str:
    display = "test" if sample == "oos" else sample
    period_key = "oos" if sample == "oos" else sample
    period = str((periods or {}).get(period_key, "")).strip()
    return f"{display} ({period})" if period else display


def resolve_sample_options(samples: list[str]) -> list[str]:
    seen = {str(s).strip() for s in (samples or []) if str(s).strip()}
    ordered = [s for s in SAMPLE_PREFERRED_ORDER if s in seen]
    extras = sorted([s for s in seen if s not in SAMPLE_PREFERRED_ORDER])
    return ordered + extras

PARAMETER_SCHEMA = {
    "data": [
        ("data_source", None, "Most upstream data origin (API or demo data)."),
        ("source", None, "Primary source identifier."),
        ("raw", None, "Raw market data feeds used for research/backtest."),
        ("features", None, "Feature set generated from raw data."),
        ("data_version", None, "Dataset version tag for reproducibility."),
        ("last_updated_utc", None, "Most recent data refresh timestamp (UTC)."),
    ],
    "trading": [
        ("timezone", None, "Timezone used in backtest and signal timestamps."),
        ("execution", None, "Order execution timing assumption."),
        ("frequency", None, "Signal/execution cadence definition."),
        ("commission", None, "Commission per side (unit: bps)."),
        ("slippage", None, "Expected slippage per trade (unit: ticks)."),
        ("position", None, "Position sizing mode."),
        ("include_night_session", None, "Whether night session is included."),
    ],
    "risk": [
        ("max_drawdown_limit", None, "Hard stop when max drawdown exceeds this level (unit: %)."),
        ("max_position", None, "Maximum concurrent position units."),
        ("stop_loss", None, "Per-trade stop loss (unit: %)."),
    ],
    "strategy": [
        ("trend_period", None, "Lookback period for trend detection."),
        ("pullback_period", None, "Lookback period for pullback confirmation."),
        ("entry_threshold", None, "Signal threshold required to trigger entry."),
    ],
}

@dataclass
class DashboardData:
    curve: pd.DataFrame
    signals: pd.DataFrame
    perf_raw: pd.DataFrame
    meta: dict[str, Any]
    ohlcv: pd.DataFrame | None = None
    trade_blotter: pd.DataFrame = None

    def __post_init__(self) -> None:
        if self.trade_blotter is None:
            self.trade_blotter = pd.DataFrame()


def _require_columns(df: pd.DataFrame, required: set[str], dataset: str) -> None:
    try:
        validate_dataframe_columns(df, required, dataset)
    except ValueError as exc:
        raise SchemaValidationError(str(exc)) from exc


def _require_perf_fields(perf_raw: pd.DataFrame) -> None:
    try:
        validate_perf_fields(perf_raw)
    except ValueError as exc:
        raise SchemaValidationError(str(exc)) from exc


def load_strategy_contexts() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "config" / "strategy_context.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def validate_strategy_context_or_raise(meta: dict[str, Any], strategy: str) -> None:
    try:
        validate_strategy_context(meta, f"strategy_context[{strategy}]")
    except ValueError as exc:
        raise SchemaValidationError(str(exc)) from exc


METRIC_COLS = [
    "total_return", "annual_return", "sharpe", "sortino", "calmar",
    "max_drawdown", "win_rate", "profit_factor", "trades",
    "avg_trade_return", "exposure_ratio",
    "payoff_ratio", "expectancy",
]


def _perf_map(perf_raw: pd.DataFrame) -> dict[str, float]:
    if perf_raw.empty:
        return {}
    row = perf_raw.iloc[0]
    out: dict[str, float] = {}
    for col in METRIC_COLS:
        if col in row.index and row[col] is not None:
            try:
                out[col] = float(row[col])
            except Exception:
                continue
    return out


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:.2%}" if abs(v) <= 1.5 else f"{v:.2f}%"


def _fmt_num(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:.2f}"


def _fmt_int(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{int(round(v))}"


def benchmark_total_return_from_curve(curve: pd.DataFrame | None) -> float | None:
    if curve is None or curve.empty or "benchmark_equity" not in curve.columns:
        return None
    series = pd.to_numeric(curve["benchmark_equity"], errors="coerce").dropna()
    if series.empty:
        return None
    first = float(series.iloc[0])
    last = float(series.iloc[-1])
    if first == 0:
        return None
    return (last / first) - 1.0


def build_general_metrics_table(perf_raw: pd.DataFrame, curve: pd.DataFrame | None = None) -> pd.DataFrame:
    pmap = _perf_map(perf_raw)
    benchmark_total_return = benchmark_total_return_from_curve(curve)

    rows = [
        {"Metric": "Total Return", "Strategy": _fmt_pct(pmap.get("total_return")), "Benchmark": _fmt_pct(benchmark_total_return)},
        {"Metric": "Annual Return", "Strategy": _fmt_pct(pmap.get("annual_return")), "Benchmark": "-"},
        {"Metric": "Sharpe", "Strategy": _fmt_num(pmap.get("sharpe")), "Benchmark": "-"},
        {"Metric": "Sortino", "Strategy": _fmt_num(pmap.get("sortino")), "Benchmark": "-"},
        {"Metric": "Max Drawdown", "Strategy": _fmt_pct(pmap.get("max_drawdown")), "Benchmark": "-"},
        {"Metric": "Calmar", "Strategy": _fmt_num(pmap.get("calmar")), "Benchmark": "-"},
        {"Metric": "Win Rate", "Strategy": _fmt_pct(pmap.get("win_rate")), "Benchmark": "-"},
        {"Metric": "Profit Factor", "Strategy": _fmt_num(pmap.get("profit_factor")), "Benchmark": "-"},
        {"Metric": "Trades", "Strategy": _fmt_int(pmap.get("trades")), "Benchmark": "-"},
    ]
    return pd.DataFrame(rows)


def to_snake_case(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"(?<!^)(?=[A-Z])", "_", text)
    text = text.replace("-", "_").replace(" ", "_").replace("/", "_").replace(".", "_")
    text = re.sub(r"_+", "_", text).strip("_")
    return text.lower()


def build_order_details(blotter: pd.DataFrame) -> pd.DataFrame:
    """Build Order Detail table from trade_blotter DataFrame."""
    _ORDER_DETAIL_COLUMNS = ["Entry Time", "Exit Time", "Position", "Entry Price", "Exit Price", "Holding Bars", "Gross Return %", "Net Return %"]

    if blotter.empty:
        return pd.DataFrame(columns=_ORDER_DETAIL_COLUMNS)

    df = blotter.copy()
    df = df.sort_values("_time", ascending=True).reset_index(drop=True)

    out = pd.DataFrame()

    # entry_time: already formatted by load_trade_blotter; fallback to "—"
    if "entry_time" in df.columns:
        out["Entry Time"] = df["entry_time"].apply(lambda v: "—" if str(v).strip() in ("", "nan", "NaT", "None") else str(v))
    elif "entry_ts" in df.columns:
        out["Entry Time"] = pd.to_datetime(df["entry_ts"], utc=True, errors="coerce").dt.strftime("%Y-%m-%d %H:%M")
        out["Entry Time"] = out["Entry Time"].apply(lambda v: "—" if str(v).strip() in ("", "nan", "NaT", "None") else str(v))
    else:
        out["Entry Time"] = pd.Series(["—"] * len(df), index=df.index)

    out["Exit Time"] = pd.to_datetime(df["_time"], utc=True, errors="coerce").dt.strftime("%Y-%m-%d %H:%M")
    side_raw = df.get("side", pd.Series(["buy"] * len(df))).astype(str).str.lower()
    qty = pd.to_numeric(df.get("quantity"), errors="coerce").round(4)
    # Merge Side + Qty → Position with sign: Long(buy) → +qty, Short(sell) → -qty
    out["Position"] = [
        f"+{q:.2f}" if s == "buy" else f"-{q:.2f}"
        for s, q in zip(side_raw, qty)
    ]
    out["Entry Price"] = pd.to_numeric(df.get("entry_price"), errors="coerce").round(2)
    out["Exit Price"] = pd.to_numeric(df.get("exit_price"), errors="coerce").round(2)
    out["Holding Bars"] = pd.to_numeric(df.get("holding_bars"), errors="coerce").astype("Int64")

    entry = pd.to_numeric(df.get("entry_price"), errors="coerce")
    exit_ = pd.to_numeric(df.get("exit_price"), errors="coerce")
    gross_return = ((exit_ - entry) / entry * 100).round(2)
    out["Gross Return %"] = gross_return

    net_pnl = pd.to_numeric(df.get("net_pnl"), errors="coerce")
    quantity = pd.to_numeric(df.get("quantity"), errors="coerce")
    net_return_pct = (net_pnl / (entry * quantity) * 100).round(2)
    out["Net Return %"] = net_return_pct

    return out[_ORDER_DETAIL_COLUMNS]


def _fmt_date_range(start: Any, end: Any) -> str:
    if not start or not end:
        return "N/A"
    return f"{start} ~ {end}"


def derive_meta_from_canonical(strategy: str, curve: pd.DataFrame, perf_raw: pd.DataFrame) -> dict[str, Any]:
    symbol = str(perf_raw.get("symbol", pd.Series(["N/A"])).iloc[0]) if not perf_raw.empty and "symbol" in perf_raw.columns else "N/A"
    timeframe = str(perf_raw.get("timeframe", pd.Series(["N/A"])).iloc[0]) if not perf_raw.empty and "timeframe" in perf_raw.columns else "N/A"
    benchmark = str(perf_raw.get("benchmark", pd.Series(["N/A"])).iloc[0]) if not perf_raw.empty and "benchmark" in perf_raw.columns else "N/A"

    full_period = "N/A"
    train_period = "N/A"
    oos_period = "N/A"
    if not curve.empty and "_time" in curve.columns:
        full_period = _fmt_date_range(curve["_time"].min().date(), curve["_time"].max().date())
        if "sample" in curve.columns:
            train_df = curve[curve["sample"] == "train"]
            oos_df = curve[curve["sample"] == "oos"]
            if not train_df.empty:
                train_period = _fmt_date_range(train_df["_time"].min().date(), train_df["_time"].max().date())
            if not oos_df.empty:
                oos_period = _fmt_date_range(oos_df["_time"].min().date(), oos_df["_time"].max().date())

    return {
        "benchmark": benchmark,
        "summary": {
            "full_sample_period": str(full_period),
            "train_period": str(train_period),
            "oos_period": str(oos_period),
            "asset": symbol,
            "freq": timeframe,
        },
        "params": {
            "signal_timeframe": timeframe,
            "execution_timeframe": timeframe,
        },
    }


def meta_context(meta: dict[str, Any]) -> dict[str, str]:
    summary = meta["summary"]
    params = meta["params"]

    signal_tf = str(params.get("signal_timeframe") or summary["freq"])
    exec_tf = str(params.get("execution_timeframe") or signal_tf)
    freq_display = f"Signal:{signal_tf} | Execution:{exec_tf}" if signal_tf != exec_tf else signal_tf

    return {
        "Date Range (Full)": str(summary["full_sample_period"]),
        "Date Range (Train)": str(summary["train_period"]),
        "Date Range (Test)": str(summary["oos_period"]),
        "Benchmark": str(meta["benchmark"]),
        "Asset": str(summary["asset"]),
        "Frequency": str(summary["freq"]),
        "FrequencyDisplay": freq_display,
    }


def build_dashboard_data(run_id: str) -> DashboardData:
    """Build DashboardData from TimescaleDB (sole data source)."""
    curve = ts_load_equity_curve(run_id)
    signals = ts_load_signals(run_id)
    perf_raw = ts_load_performance(run_id)
    ohlcv = ts_load_ohlcv(run_id)
    trade_blotter = ts_load_trade_blotter(run_id)

    # Derive strategy name from perf_raw
    strategy = str(perf_raw["strategy"].iloc[0]) if not perf_raw.empty and "strategy" in perf_raw.columns else "unknown"

    contexts = load_strategy_contexts()
    meta = contexts.get(strategy)
    if isinstance(meta, dict):
        validate_strategy_context_or_raise(meta, strategy)
    else:
        meta = derive_meta_from_canonical(strategy, curve, perf_raw)
    return DashboardData(curve=curve, signals=signals, perf_raw=perf_raw, meta=meta, ohlcv=ohlcv, trade_blotter=trade_blotter)


def render_performance_tab(data: DashboardData, overview_ctx: dict[str, str], alpha_value: str, strategy_logic: str) -> None:
    st.markdown("### Strategy Overview")
    st.markdown(
        f"<div class='overview-desc'>{strategy_logic}</div>",
        unsafe_allow_html=True,
    )

    full_period = overview_ctx.get("Date Range (Full)", "N/A")
    if "~" in full_period:
        start, end = [x.strip() for x in full_period.split("~", 1)]
    elif " to " in full_period:
        start, end = [x.strip() for x in full_period.split(" to ", 1)]
    else:
        start, end = full_period, ""

    c1, c2, c3, c4 = st.columns(4)
    test_start = overview_ctx.get("Date Range (Test)", "N/A")
    with c1:
        st.markdown(
            f"""
            <div class='metric-card'>
                <div class='metric-label'>Full Period</div>
                <div class='metric-value'>{start} ~ {end}</div>
                <div class='metric-sub'>Test Period: {test_start}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            f"""
            <div class='metric-card'>
                <div class='metric-label'>Asset</div>
                <div class='metric-value'>{overview_ctx.get('Asset', 'N/A')}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            f"""
            <div class='metric-card'>
                <div class='metric-label'>Frequency</div>
                <div class='metric-value'>{overview_ctx.get('FrequencyDisplay', overview_ctx.get('Frequency', 'N/A'))}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c4:
        st.markdown(
            f"""
            <div class='metric-card'>
                <div class='metric-label'>Alpha</div>
                <div class='metric-value-alpha'>{alpha_value or 'N/A'}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    top_left, top_right = st.columns(2)
    with top_left:
        st.markdown("#### Asset Price With Trading Signals")
        has_ohlcv = data.ohlcv is not None and not data.ohlcv.empty and "close" in data.ohlcv.columns
        has_signals = not data.signals.empty and "price" in data.signals.columns
        if not has_ohlcv and not has_signals:
            st.info("No signal/price data available for this selection.")
        else:
            fig = go.Figure()

            # Draw continuous OHLCV close as base line
            if has_ohlcv:
                fig.add_trace(go.Scatter(
                    x=data.ohlcv["_time"],
                    y=data.ohlcv["close"].astype(float),
                    mode="lines",
                    name="Close Price",
                    line=dict(width=1.5, color="#94A3B8"),
                ))

            # Overlay buy/sell signal markers
            if has_signals and "signal_strength" in data.signals.columns:
                signal_strength = data.signals["signal_strength"].astype(float)
                buy_mask = signal_strength > 0
                sell_mask = signal_strength < 0
                if buy_mask.any():
                    fig.add_trace(go.Scatter(
                        x=data.signals.loc[buy_mask, "_time"],
                        y=data.signals.loc[buy_mask, "price"].astype(float),
                        mode="markers",
                        name="Buy Signal",
                        marker=dict(symbol="triangle-up", size=10, color="#10B981"),
                    ))
                if sell_mask.any():
                    fig.add_trace(go.Scatter(
                        x=data.signals.loc[sell_mask, "_time"],
                        y=data.signals.loc[sell_mask, "price"].astype(float),
                        mode="markers",
                        name="Sell Signal",
                        marker=dict(symbol="triangle-down", size=10, color="#EF4444"),
                    ))

            fig.update_layout(height=CHART_HEIGHT_PRICE, margin=dict(l=10, r=10, t=10, b=10), plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", xaxis=dict(gridcolor="#F1F5F9"), yaxis=dict(gridcolor="#F1F5F9"), legend=dict(orientation="h", yanchor="top", y=-0.2, xanchor="left", x=0))
            st.plotly_chart(fig, use_container_width=True)

    with top_right:
        st.markdown("#### Performance Analysis")
        if data.perf_raw.empty:
            st.info("No performance metrics available for this selection.")
        else:
            pmap = _perf_map(data.perf_raw)
            perf_rows = [
                {"Metric": "Total Return", "Strategy": _fmt_pct(pmap.get("total_return")), "Benchmark": "-"},
                {"Metric": "Annual Return", "Strategy": _fmt_pct(pmap.get("annual_return")), "Benchmark": "-"},
                {"Metric": "Sharpe", "Strategy": _fmt_num(pmap.get("sharpe")), "Benchmark": "-"},
                {"Metric": "Sortino", "Strategy": _fmt_num(pmap.get("sortino")), "Benchmark": "-"},
                {"Metric": "Max Drawdown", "Strategy": _fmt_pct(pmap.get("max_drawdown")), "Benchmark": "-"},
                {"Metric": "Calmar", "Strategy": _fmt_num(pmap.get("calmar")), "Benchmark": "-"},
                {"Metric": "Win Rate", "Strategy": _fmt_pct(pmap.get("win_rate")), "Benchmark": "-"},
                {"Metric": "Profit Factor", "Strategy": _fmt_num(pmap.get("profit_factor")), "Benchmark": "-"},
                {"Metric": "Payoff Ratio", "Strategy": _fmt_num(pmap.get("payoff_ratio")), "Benchmark": "-"},
                {"Metric": "Expectancy", "Strategy": _fmt_num(pmap.get("expectancy")), "Benchmark": "-"},
                {"Metric": "Trades", "Strategy": _fmt_int(pmap.get("trades")), "Benchmark": "-"},
            ]
            perf_df = pd.DataFrame(perf_rows)
            st.dataframe(perf_df, use_container_width=True, hide_index=True, height=TABLE_HEIGHT_PERF)

    bottom_left, bottom_right = st.columns(2)
    with bottom_left:
        st.markdown("#### Cumulative Return: Strategy vs. Benchmark")
        if data.curve.empty:
            st.info("No equity curve data available for this selection.")
        else:
            strategy_curve = data.curve.get("equity", data.curve.get("nav", pd.Series(dtype=float)))
            if strategy_curve.empty:
                st.info("No strategy equity series available.")
            else:
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=data.curve["_time"], y=strategy_curve.astype(float) - 1.0, mode="lines", name="Strategy", line=dict(width=2.5, color="#2563EB")))
                if "benchmark_equity" in data.curve.columns:
                    benchmark_ret = data.curve["benchmark_equity"].astype(float) - 1.0
                    if benchmark_ret.notna().any():
                        fig.add_trace(go.Scatter(
                            x=data.curve["_time"],
                            y=benchmark_ret,
                            mode="lines",
                            name="Benchmark",
                            line=dict(width=1.5, color="#94A3B8", dash="dash"),
                        ))
                fig.update_layout(height=CHART_HEIGHT_RETURN, margin=dict(l=10, r=10, t=10, b=10), yaxis_tickformat=".1%", plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", xaxis=dict(gridcolor="#F1F5F9"), yaxis=dict(gridcolor="#F1F5F9"), legend=dict(orientation="h", yanchor="top", y=-0.2, xanchor="left", x=0))
                st.plotly_chart(fig, use_container_width=True)

    with bottom_right:
        st.markdown("#### Order Details")
        order_df = build_order_details(data.trade_blotter)
        if order_df.empty:
            st.info("No trade blotter data available for this selection.")
        else:
            def _color_return(val: float) -> str:
                try:
                    v = float(val)
                except (TypeError, ValueError):
                    return ""
                return "color: #10B981; font-weight: 600" if v > 0 else ("color: #EF4444; font-weight: 600" if v < 0 else "")

            styled = order_df.style.applymap(_color_return, subset=["Gross Return %", "Net Return %"]).format(
                {"Entry Price": "{:.2f}", "Exit Price": "{:.2f}", "Gross Return %": "{:+.2f}%", "Net Return %": "{:+.2f}%"}
            )
            _COLUMN_ORDER = ["Entry Time", "Exit Time", "Position", "Entry Price", "Exit Price", "Holding Bars", "Gross Return %", "Net Return %"]
            st.dataframe(
                styled,
                use_container_width=True,
                hide_index=True,
                height=TABLE_HEIGHT_PERF,
                column_order=_COLUMN_ORDER,
            )


def render_parameter_tab(meta: dict[str, Any]) -> None:
    def _flatten(prefix: str, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for k, v in value.items():
                key_snake = to_snake_case(k)
                key = f"{prefix}.{key_snake}" if prefix else key_snake
                out.update(_flatten(key, v))
            return out
        return {prefix: value}

    def _actual_values(group: str) -> dict[str, Any]:
        params = meta.get("params", {}) or {}
        summary = meta.get("summary", {}) or {}
        cost_model = meta.get("cost_model", {}) or {}
        session_rules = meta.get("session_rules", {}) or {}
        risk_limits = meta.get("risk_limits", {}) or {}
        data_block = meta.get("data", {}) or {}

        if group == "data":
            return {
                "data_source": meta.get("data_source"),
                "source": data_block.get("source"),
                "raw": data_block.get("raw"),
                "features": data_block.get("features"),
                "data_version": meta.get("data_version"),
                "last_updated_utc": meta.get("last_updated_utc"),
            }

        if group == "trading":
            return {
                "timezone": params.get("timezone"),
                "execution": params.get("execution"),
                "frequency": summary.get("frequency"),
                "commission": cost_model.get("commission"),
                "slippage": cost_model.get("slippage"),
                "position": params.get("position"),
                "include_night_session": session_rules.get("include_night_session"),
            }

        if group == "risk":
            return {
                "max_drawdown_limit": risk_limits.get("max_drawdown_limit"),
                "max_position": risk_limits.get("max_position"),
                "stop_loss": risk_limits.get("stop_loss"),
            }

        if group == "strategy":
            excluded = {
                "timeframe",
                "signal_timeframe",
                "execution_timeframe",
                "execution",
                "timezone",
                "position",
                "features",
            }
            out: dict[str, Any] = {}
            for raw_key, value in params.items():
                key_snake = to_snake_case(raw_key)
                if key_snake in excluded:
                    continue
                out[key_snake] = value
            return out

        return {}

    intro_map = {
        "data": "Core dataset lineage and feature mapping for this strategy run.",
        "trading": "Execution and cost assumptions used for simulation and monitoring.",
        "risk": "Risk guardrails and safety constraints applied to this strategy.",
        "strategy": "Strategy parameters only (logic narrative and trading frequency excluded).",
    }

    def _build_rows(group: str) -> pd.DataFrame:
        schema = PARAMETER_SCHEMA.get(group, [])
        values = {to_snake_case(k): v for k, v in _actual_values(group).items()}
        desc_map = {to_snake_case(k): d for k, _, d in schema}

        rows = []
        used = set()
        for key, _example_value, desc in schema:
            key_snake = to_snake_case(key)
            used.add(key_snake)
            actual = values.get(key_snake)
            rows.append({
                "Key": key_snake,
                "Value": actual if actual not in (None, "") else PARAM_DEFAULT_VALUE,
                "Description": desc,
            })

        for key, value in values.items():
            if key in used:
                continue
            rows.append({
                "Key": to_snake_case(key),
                "Value": value if value not in (None, "") else PARAM_DEFAULT_VALUE,
                "Description": desc_map.get(key, PARAM_DEFAULT_DESCRIPTION),
            })

        df = pd.DataFrame(rows, columns=PARAM_TABLE_COLUMNS)
        return df

    row1_col1, row1_col2 = st.columns(2)
    with row1_col1:
        st.markdown("**data**")
        st.caption(intro_map["data"])
        st.dataframe(_build_rows("data"), use_container_width=True, hide_index=True, height=TABLE_HEIGHT_PARAM,
            column_config={
                "Key": st.column_config.TextColumn("Key", width="medium"),
                "Value": st.column_config.TextColumn("Value", width="medium"),
                "Description": st.column_config.TextColumn("Description", width="medium"),
            },
        )
    with row1_col2:
        st.markdown("**trading**")
        st.caption(intro_map["trading"])
        st.dataframe(_build_rows("trading"), use_container_width=True, hide_index=True, height=TABLE_HEIGHT_PARAM,
            column_config={
                "Key": st.column_config.TextColumn("Key", width="medium"),
                "Value": st.column_config.TextColumn("Value", width="medium"),
                "Description": st.column_config.TextColumn("Description", width="medium"),
            },
        )

    row2_col1, row2_col2 = st.columns(2)
    with row2_col1:
        st.markdown("**risk**")
        st.caption(intro_map["risk"])
        st.dataframe(_build_rows("risk"), use_container_width=True, hide_index=True, height=TABLE_HEIGHT_PARAM,
            column_config={
                "Key": st.column_config.TextColumn("Key", width="medium"),
                "Value": st.column_config.TextColumn("Value", width="medium"),
                "Description": st.column_config.TextColumn("Description", width="medium"),
            },
        )
    with row2_col2:
        st.markdown("**strategy**")
        st.caption(intro_map["strategy"])
        st.dataframe(_build_rows("strategy"), use_container_width=True, hide_index=True, height=TABLE_HEIGHT_PARAM,
            column_config={
                "Key": st.column_config.TextColumn("Key", width="medium"),
                "Value": st.column_config.TextColumn("Value", width="medium"),
                "Description": st.column_config.TextColumn("Description", width="medium"),
            },
        )


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)
    st.caption(APP_CAPTION)
    st.markdown("<div style='font-size:12px;color:#64748B;margin-bottom:4px;'>Last Updated: auto (run-dependent)</div>", unsafe_allow_html=True)
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=JetBrains+Mono:wght@500;700&display=swap');

        :root {
            --primary: #1E3A8A;
            --success: #10B981;
            --danger: #EF4444;
            --warning: #F59E0B;
            --bg-main: #F8FAFC;
            --grid: #F1F5F9;
            --text-main: #0F172A;
            --muted: #475569;
            --radius: 10px;
            --border: #E2E8F0;
        }

        html, body, [class*="css"] { font-family: 'Inter', sans-serif; background: var(--bg-main); color: var(--text-main); }
        .stApp {background-color: var(--bg-main) !important;}
        section.main > div {background-color: var(--bg-main) !important;}
        .overview-desc {font-size:14px; color:#666666; margin-bottom:10px;}
        .metric-card {background:#FFFFFF; border:1px solid var(--border); border-radius:var(--radius); padding:16px 18px; min-height:120px; box-shadow:0 4px 10px rgba(15,23,42,.04);}
        .metric-label {font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:#8E8E93; margin-bottom:6px;}
        .metric-value {font-size:20px; font-weight:700; color:#1C1C1E; line-height:1.35; font-family:'JetBrains Mono', monospace;}
        .metric-sub {font-size:12px; color:#666666; margin-top:6px;}
        .metric-value-alpha {font-size:20px; font-weight:700; color:#1E3A8A; line-height:1.35; font-family:'JetBrains Mono', monospace;}

        .stTabs [data-baseweb="tab-list"] { gap: 18px; }
        .stTabs [data-baseweb="tab"] { height: 44px; color: var(--muted); font-weight: 600; }
        .stTabs [aria-selected="true"] { color: var(--primary) !important; border-bottom-color: var(--primary) !important; }

        [data-testid="stMetricValue"] { font-family: 'JetBrains Mono', monospace !important; }
        div[data-baseweb="notification"]{background:#F5F7FA !important; border:1px solid #E2E8F0 !important; color:#334155 !important;}

        @media (max-width: 768px) {
            .metric-card { min-height: 92px; padding: 12px; }
            .metric-value, .metric-value-alpha { font-size: 16px; }
            .metric-sub { font-size: 11px; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # --- Sidebar: Run selector from TimescaleDB ---
    try:
        runs_df = ts_list_runs(limit=50)
    except Exception as e:
        st.error(f"TimescaleDB 連線失敗: {e}")
        st.stop()

    if runs_df.empty:
        st.warning("No backtest runs found in TimescaleDB.")
        st.stop()

    st.sidebar.header("Filters")

    # Strategy selector
    strategies = sorted(runs_df["strategy"].dropna().unique().tolist())
    if not strategies:
        st.warning("No strategies found.")
        st.stop()
    strategy = st.sidebar.selectbox("Strategy", strategies, index=0)

    # Filter runs by strategy
    strategy_runs = runs_df[runs_df["strategy"] == strategy]

    # Sample selector
    contexts_preview = load_strategy_contexts()
    meta_preview = contexts_preview.get(strategy)
    has_strategy_context = isinstance(meta_preview, dict)
    periods_preview = {}
    if has_strategy_context:
        try:
            validate_strategy_context_or_raise(meta_preview, strategy)
            periods_preview = meta_preview.get("periods", {}) or {}
        except SchemaValidationError as e:
            st.error(f"Schema validation failed: {e}")
            st.stop()

    sample_values = strategy_runs["sample"].dropna().unique().tolist()
    sample_options = resolve_sample_options(sample_values)
    if not sample_options:
        st.warning("No sample found for selected strategy.")
        st.stop()

    sample = st.sidebar.selectbox(
        "Sample",
        sample_options,
        index=0,
        format_func=lambda x: sample_label(x, periods_preview),
    )

    # Run ID selector
    if sample != "full":
        filtered_runs = strategy_runs[strategy_runs["sample"] == sample]
    else:
        filtered_runs = strategy_runs

    run_ids = filtered_runs["run_id"].tolist()
    if not run_ids:
        st.warning("No run_id found for selected strategy/sample.")
        st.stop()
    run_id = st.sidebar.selectbox("Run ID", run_ids, index=0)

    # --- Load data ---
    try:
        data = build_dashboard_data(run_id)
    except SchemaValidationError as e:
        st.error(f"Schema validation failed: {e}")
        st.stop()
    except Exception as e:
        st.error(f"TimescaleDB 連線失敗: {e}")
        st.stop()

    context = meta_context(data.meta)
    strategy_logic = str(data.meta.get("logic") or "No strategy logic provided.")
    alpha_value = "N/A"
    if not data.perf_raw.empty:
        perf_map = _perf_map(data.perf_raw)
        strategy_ret = perf_map.get("total_return")
        benchmark_ret = benchmark_total_return_from_curve(data.curve)
        if strategy_ret is not None and benchmark_ret is not None:
            alpha_value = f"{(strategy_ret - benchmark_ret):.2%}"

    tab_performance, tab_parameters = st.tabs(["Performance", "Parameter"])

    with tab_performance:
        render_performance_tab(data, context, alpha_value, strategy_logic)

    with tab_parameters:
        if not has_strategy_context:
            st.warning("Blocker: strategy_context.json missing this strategy. Parameter tab can only show canonical-derived minimal mapping.")
        render_parameter_tab(data.meta)



if __name__ == "__main__":
    main()
