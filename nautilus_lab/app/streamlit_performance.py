from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nautilus_lab.contracts import SCHEMA_VERSION, validate_dataframe_columns, validate_perf_fields, validate_strategy_context


class SchemaValidationError(ValueError):
    """Raised when payload does not match expected canonical schema."""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from influxdb_client import InfluxDBClient


APP_TITLE = "Strategy Backtest Analysis"
APP_CAPTION = "UI build: 2026-03-06-1200"

TABLE_HEIGHT_PERF = 260
TABLE_HEIGHT_PARAM = 280
CHART_HEIGHT_PRICE = 300
CHART_HEIGHT_RETURN = 230

PARAM_TABLE_COLUMNS = ["Key", "Value", "Description"]
PARAM_DEFAULT_VALUE = "MISSING"
PARAM_DEFAULT_DESCRIPTION = "Reserved for future extension."

SAMPLE_OPTIONS = ["oos", "train", "full"]

REQUIRED_SIGNAL_COLUMNS = {"_time", "price", "signal_strength", "side", "run_id", "strategy"}
REQUIRED_CURVE_COLUMNS = {"_time", "equity", "run_id", "strategy", "sample"}
def sample_label(sample: str, periods: dict[str, Any]) -> str:
    display = "test" if sample == "oos" else sample
    period_key = "oos" if sample == "oos" else sample
    period = str((periods or {}).get(period_key, "")).strip()
    return f"{display} ({period})" if period else display

PERF_METRIC_MAP = {
    "total_return": ("Total Return (Active Period)", "Strategy"),
    "bh_total_return": ("Total Return (Active Period)", "Benchmark"),
    "max_drawdown": ("Max Drawdown (Active Period)", "Strategy"),
    "profit_factor": ("Profit Factor", "Strategy"),
    "win_rate": ("Win Rate", "Strategy"),
    "avg_trade_return": ("Return Per Trade", "Strategy"),
    "trades": ("Trades", "Strategy"),
    "exposure_ratio": ("Exposure Ratio", "Strategy"),
}

PERF_METRIC_ORDER = [
    "Total Return (Active Period)",
    "Max Drawdown (Active Period)",
    "Volatility (Active Period)",
    "Total Return (Full Period)",
    "Max Drawdown (Full Period)",
    "Volatility (Full Period)",
    "Trades",
    "Active Observations",
    "Exposure Ratio",
    "Profit Factor",
    "Return Per Trade",
    "Win Rate",
]
METRIC_DEFINITIONS = {
    "Total Return": "Net return over the selected period.",
    "Max Drawdown": "Largest peak-to-trough decline in portfolio value.",
    "Volatility": "Standard deviation of returns over the selected period.",
    "Total Return (Active Period)": "Return measured only when strategy is active (holding position).",
    "Max Drawdown (Active Period)": "Max drawdown measured on active periods only.",
    "Volatility (Active Period)": "Return volatility measured on active periods only.",
    "Total Return (Full Period)": "Return measured over full backtest horizon.",
    "Max Drawdown (Full Period)": "Max drawdown measured over full backtest horizon.",
    "Volatility (Full Period)": "Return volatility measured over full backtest horizon.",
    "Trades": "Number of round-trip transactions.",
    "Profit Factor": "Gross profit divided by gross loss.",
    "Win Rate": "Winning trades divided by total trades.",
    "Return Per Trade": "Average return per round-trip trade.",
    "Exposure Ratio": "Active observations divided by total observations.",
    "Active Observations": "Periods holding position (either long or short).",
}


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

METRIC_FORMULAS = {
    "Total Return": "(Ending Equity / Starting Equity) - 1",
    "Max Drawdown": "min((Equity - RunningMaxEquity) / RunningMaxEquity)",
    "Volatility": "StdDev(Periodic Returns)",
    "Total Return (Active Period)": "Return computed on active-position periods only",
    "Max Drawdown (Active Period)": "MDD computed on active-position periods only",
    "Volatility (Active Period)": "Volatility computed on active-position periods only",
    "Total Return (Full Period)": "Return computed over full backtest horizon",
    "Max Drawdown (Full Period)": "MDD computed over full backtest horizon",
    "Volatility (Full Period)": "Volatility over full backtest horizon",
    "Trades": "Count(Round-Trip Transactions)",
    "Profit Factor": "Gross Profit / Gross Loss",
    "Win Rate": "Winning Trades / Total Trades",
    "Return Per Trade": "Sum(Trade Returns) / Trades",
    "Exposure Ratio": "Active Observations / Total Observations",
    "Active Observations": "Count(Periods with non-zero position)",
}


def ordered_metrics(metric_values: list[str]) -> list[str]:
    existing = set(metric_values)
    return [metric for metric in PERF_METRIC_ORDER if metric in existing]


def normalize_position(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("_", " ")
    aliases = {
        "long": "buy",
        "cover": "buy to cover",
        "short": "sell short",
        "short sell": "sell short",
        "close short": "buy to cover",
        "exit short": "buy to cover",
    }
    normalized = aliases.get(raw, raw)
    allowed = {"buy", "sell", "sell short", "buy to cover"}
    return normalized if normalized in allowed else "unknown"


@dataclass
class InfluxCfg:
    url: str
    org: str
    bucket: str
    token: str


@dataclass
class DashboardData:
    curve: pd.DataFrame
    signals: pd.DataFrame
    perf_raw: pd.DataFrame
    meta: dict[str, Any]


def get_cfg() -> InfluxCfg:
    return InfluxCfg(
        url=os.getenv("INFLUX_URL", "http://localhost:8086"),
        org=os.getenv("INFLUX_ORG", "quant_research"),
        bucket=os.getenv("INFLUX_BUCKET", "nautilus_signals"),
        token=os.getenv("INFLUX_TOKEN") or os.getenv("DOCKER_INFLUXDB_INIT_ADMIN_TOKEN", ""),
    )


def query_df(client: InfluxDBClient, org: str, flux: str) -> pd.DataFrame:
    frames = client.query_api().query_data_frame(query=flux, org=org)
    if isinstance(frames, list):
        frames = [f for f in frames if isinstance(f, pd.DataFrame) and not f.empty]
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)
    return frames if isinstance(frames, pd.DataFrame) else pd.DataFrame()


def tag_values(client: InfluxDBClient, cfg: InfluxCfg, measurement: str, tag: str, predicate: str | None = None) -> list[str]:
    pred = f'r._measurement == "{measurement}"'
    if predicate:
        pred = f"{pred} and ({predicate})"
    flux = f'''
import "influxdata/influxdb/schema"
schema.tagValues(bucket: "{cfg.bucket}", tag: "{tag}", predicate: (r) => {pred}, start: -365d)
'''
    df = query_df(client, cfg.org, flux)
    if df.empty or "_value" not in df.columns:
        return []
    return sorted([str(v) for v in df["_value"].dropna().unique()])


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


def _sample_filter(sample: str) -> str:
    return "true" if sample == "full" else f'r.sample == "{sample}"'


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


def load_curve(client: InfluxDBClient, cfg: InfluxCfg, strategy: str, sample: str, run_id: str) -> pd.DataFrame:
    flux = f'''
from(bucket: "{cfg.bucket}")
  |> range(start: -365d)
  |> filter(fn:(r)=> r._measurement=="perf_equity_curve")
  |> filter(fn:(r)=> r.schema_version == "{SCHEMA_VERSION}")
  |> filter(fn:(r)=> r.strategy == "{strategy}")
  |> filter(fn:(r)=> {_sample_filter(sample)})
  |> filter(fn:(r)=> r.run_id == "{run_id}")
  |> filter(fn:(r)=> r._field == "equity" or r._field == "benchmark_equity" or r._field == "drawdown")
  |> pivot(rowKey:["_time","strategy","sample","run_id"], columnKey:["_field"], valueColumn:"_value")
  |> sort(columns:["_time"], desc:false)
'''
    df = query_df(client, cfg.org, flux)
    if df.empty:
        return df

    _require_columns(df, REQUIRED_CURVE_COLUMNS, "perf_equity_curve")
    df["_time"] = pd.to_datetime(df["_time"], utc=True)
    return df


def load_signals(client: InfluxDBClient, cfg: InfluxCfg, strategy: str, run_id: str) -> pd.DataFrame:
    flux = f'''
from(bucket: "{cfg.bucket}")
  |> range(start: -365d)
  |> filter(fn:(r)=> r._measurement=="strategy_signals")
  |> filter(fn:(r)=> r.schema_version == "{SCHEMA_VERSION}")
  |> filter(fn:(r)=> r.strategy == "{strategy}")
  |> filter(fn:(r)=> r.run_id == "{run_id}")
  |> filter(fn:(r)=> r._field == "signal_strength" or r._field == "price")
  |> pivot(rowKey:["_time","strategy","run_id","side"], columnKey:["_field"], valueColumn:"_value")
  |> sort(columns:["_time"], desc:false)
'''
    df = query_df(client, cfg.org, flux)
    if not df.empty:
        _require_columns(df, REQUIRED_SIGNAL_COLUMNS, "strategy_signals")
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
    return df


def load_perf(client: InfluxDBClient, cfg: InfluxCfg, strategy: str, sample: str, run_id: str) -> pd.DataFrame:
    flux = f'''
from(bucket: "{cfg.bucket}")
  |> range(start: -365d)
  |> filter(fn:(r)=> r._measurement=="strategy_performance")
  |> filter(fn:(r)=> r.schema_version == "{SCHEMA_VERSION}")
  |> filter(fn:(r)=> r.strategy == "{strategy}")
  |> filter(fn:(r)=> {_sample_filter(sample)})
  |> filter(fn:(r)=> r.run_id == "{run_id}")
  |> filter(fn:(r)=> r._field == "total_return" or r._field == "max_drawdown" or r._field == "profit_factor" or r._field == "win_rate" or r._field == "avg_trade_return" or r._field == "trades" or r._field == "exposure_ratio" or r._field == "bh_total_return")
  |> last()
'''
    df = query_df(client, cfg.org, flux)
    _require_perf_fields(df)
    return df


def _perf_map(perf_raw: pd.DataFrame) -> dict[str, float]:
    out: dict[str, float] = {}
    for _, r in perf_raw.iterrows():
        key = str(r.get("_field"))
        try:
            out[key] = float(r.get("_value", 0.0))
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


def build_general_metrics_table(perf_raw: pd.DataFrame) -> pd.DataFrame:
    pmap = _perf_map(perf_raw)

    s_total_active = pmap.get("active_total_return")
    b_total_active = pmap.get("bh_active_total_return")
    s_mdd_active = pmap.get("active_max_drawdown")
    b_mdd_active = pmap.get("bh_active_max_drawdown")
    s_vol_active = pmap.get("active_volatility")
    b_vol_active = pmap.get("bh_active_volatility")

    s_total_full = pmap.get("total_return")
    b_total_full = pmap.get("bh_total_return")
    s_mdd_full = pmap.get("max_drawdown")
    b_mdd_full = pmap.get("bh_max_drawdown")
    s_vol_full = pmap.get("volatility")
    b_vol_full = pmap.get("bh_volatility")

    rows = [
        {"Metric": "Total Return (Active Period)", "Strategy": _fmt_pct(s_total_active), "Benchmark": _fmt_pct(b_total_active), "Highlight": ""},
        {"Metric": "Max Drawdown (Active Period)", "Strategy": _fmt_pct(s_mdd_active), "Benchmark": _fmt_pct(b_mdd_active), "Highlight": ""},
        {"Metric": "Volatility (Active Period)", "Strategy": _fmt_pct(s_vol_active), "Benchmark": _fmt_pct(b_vol_active), "Highlight": ""},
        {"Metric": "Total Return (Full Period)", "Strategy": _fmt_pct(s_total_full), "Benchmark": _fmt_pct(b_total_full), "Highlight": ""},
        {"Metric": "Max Drawdown (Full Period)", "Strategy": _fmt_pct(s_mdd_full), "Benchmark": _fmt_pct(b_mdd_full), "Highlight": ""},
        {"Metric": "Volatility (Full Period)", "Strategy": _fmt_pct(s_vol_full), "Benchmark": _fmt_pct(b_vol_full), "Highlight": ""},
    ]
    return pd.DataFrame(rows)


def build_strategy_specific_table(perf_raw: pd.DataFrame) -> pd.DataFrame:
    pmap = _perf_map(perf_raw)
    trades = pmap.get("trades")
    win_rate = pmap.get("win_rate")
    avg_trade_return = pmap.get("avg_trade_return")
    profit_factor = pmap.get("profit_factor")
    exposure = pmap.get("exposure_ratio")
    active_obs = pmap.get("active_observations")
    total_obs = pmap.get("total_observations")

    if exposure is None and active_obs is not None and total_obs not in (None, 0):
        exposure = active_obs / total_obs

    rows = [
        {"Metric": "Trades", "Value": _fmt_int(trades), "Definition": "Number Of Round Trip Transactions"},
        {"Metric": "Profit Factor", "Value": _fmt_num(profit_factor), "Definition": "Gross Profit Divided By Gross Loss"},
        {"Metric": "Win Rate", "Value": _fmt_pct(win_rate), "Definition": "Winning Trades Divided By Total Trades"},
        {"Metric": "Return Per Trade", "Value": _fmt_pct(avg_trade_return), "Definition": "Average Return Per Round Trip Trade"},
        {"Metric": "Exposure Ratio", "Value": _fmt_pct(exposure), "Definition": "Active Observations Divided By Total Observations"},
        {"Metric": "Active Observations", "Value": _fmt_int(active_obs), "Definition": "Periods Holding Position (Either Long Or Short)"},
    ]
    return pd.DataFrame(rows)


def build_perf_table(perf_raw: pd.DataFrame) -> pd.DataFrame:
    # backward-compat helper; unused after split tables
    return build_general_metrics_table(perf_raw)


def to_snake_case(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"(?<!^)(?=[A-Z])", "_", text)
    text = text.replace("-", "_").replace(" ", "_").replace("/", "_").replace(".", "_")
    text = re.sub(r"_+", "_", text).strip("_")
    return text.lower()


def flatten_dict(prefix: str, data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        key_snake = to_snake_case(key)
        full_key = f"{prefix}.{key_snake}" if prefix else key_snake
        if isinstance(value, dict):
            out.update(flatten_dict(full_key, value))
        else:
            out[full_key] = value
    return out


def kv_to_df(kv: dict[str, Any], descriptions: dict[str, str] | None = None) -> pd.DataFrame:
    descriptions = descriptions or {}
    rows = []
    for key, raw_value in kv.items():
        value = ", ".join(map(str, raw_value)) if isinstance(raw_value, list) else raw_value
        display_value = PARAM_DEFAULT_VALUE if value in (None, "") else value
        rows.append(
            {
                "Key": str(key),
                "Value": display_value,
                "Description": descriptions.get(str(key), PARAM_DEFAULT_DESCRIPTION),
            }
        )
    if not rows:
        rows.append({"Key": "placeholder", "Value": PARAM_DEFAULT_VALUE, "Description": PARAM_DEFAULT_DESCRIPTION})
    return pd.DataFrame(rows, columns=PARAM_TABLE_COLUMNS)


def build_order_details(signals: pd.DataFrame) -> pd.DataFrame:
    if signals.empty:
        return pd.DataFrame(columns=["Trade ID", "Time", "Position", "Execution Price", "Gross PnL"])

    out = pd.DataFrame()
    out["Time"] = pd.to_datetime(signals.get("_time", pd.Series([], dtype="datetime64[ns]")), utc=True, errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    side = signals.get("side", pd.Series(["buy"] * len(signals))).astype(str)
    out["Position"] = side.apply(normalize_position)
    out["Execution Price"] = pd.to_numeric(signals.get("price", pd.Series([None] * len(signals))), errors="coerce")
    out["Gross PnL"] = "N/A"

    trade_id = 0
    active_trade = None
    tids = []
    for pos in out["Position"].tolist():
        if pos in {"buy", "sell short"}:
            trade_id += 1
            active_trade = f"T{trade_id:04d}"
            tids.append(active_trade)
        else:
            if active_trade is None:
                trade_id += 1
                active_trade = f"T{trade_id:04d}"
            tids.append(active_trade)
            active_trade = None
    out["Trade ID"] = tids
    out = out.dropna(subset=["Time"]).sort_values("Time", ascending=False).reset_index(drop=True)
    return out[["Trade ID", "Time", "Position", "Execution Price", "Gross PnL"]]


def render_kv_table(
    title: str,
    description: str,
    kv: dict[str, Any],
    field_descriptions: dict[str, str],
    height: int = TABLE_HEIGHT_PARAM,
) -> None:
    with st.container(border=True):
        st.markdown(f"**{title}**")
        st.caption(description)
        st.dataframe(
            kv_to_df(kv, field_descriptions),
            use_container_width=True,
            hide_index=True,
            height=height,
            column_config={
                "Key": st.column_config.TextColumn("Key", width="medium"),
                "Value": st.column_config.TextColumn("Value", width="medium"),
                "Description": st.column_config.TextColumn("Description", width="medium"),
            },
        )


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



def build_dashboard_data(client: InfluxDBClient, cfg: InfluxCfg, strategy: str, sample: str, run_id: str) -> DashboardData:
    curve = load_curve(client, cfg, strategy, sample, run_id)
    signals = load_signals(client, cfg, strategy, run_id)
    perf_raw = load_perf(client, cfg, strategy, sample, run_id)

    contexts = load_strategy_contexts()
    meta = contexts.get(strategy)
    if not isinstance(meta, dict):
        raise SchemaValidationError(f"strategy_context missing strategy: {strategy}")
    validate_strategy_context_or_raise(meta, strategy)
    return DashboardData(curve=curve, signals=signals, perf_raw=perf_raw, meta=meta)


def render_price_signal_chart(signals: pd.DataFrame) -> None:
    st.markdown("#### Raw Price + Signals")
    if signals.empty or "price" not in signals.columns:
        st.info("No signal/price data available for this selection.")
        return

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=signals["_time"], y=signals["price"].astype(float), mode="lines", name="Raw Price"))

    if "signal_strength" in signals.columns:
        signal_strength = signals["signal_strength"].astype(float)
        buy_mask = signal_strength > 0
        sell_mask = signal_strength < 0

        if buy_mask.any():
            fig.add_trace(
                go.Scatter(
                    x=signals.loc[buy_mask, "_time"],
                    y=signals.loc[buy_mask, "price"].astype(float),
                    mode="markers",
                    name="Buy Signal",
                    marker=dict(symbol="triangle-up", size=10, color="#10B981"),
                )
            )
        if sell_mask.any():
            fig.add_trace(
                go.Scatter(
                    x=signals.loc[sell_mask, "_time"],
                    y=signals.loc[sell_mask, "price"].astype(float),
                    mode="markers",
                    name="Sell Signal",
                    marker=dict(symbol="triangle-down", size=10, color="#EF4444"),
                )
            )

    fig.update_layout(height=CHART_HEIGHT_PRICE, margin=dict(l=10, r=10, t=10, b=10), plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", xaxis=dict(gridcolor="#F1F5F9"), yaxis=dict(gridcolor="#F1F5F9"), legend=dict(orientation="h", yanchor="top", y=-0.2, xanchor="left", x=0))
    st.plotly_chart(fig, use_container_width=True)


def render_cumulative_return_chart(curve: pd.DataFrame) -> None:
    st.markdown("#### Cumulative Return: Strategy vs Benchmark")
    if curve.empty:
        st.info("No equity curve data available for this selection.")
        return

    strategy_curve = curve.get("equity", curve.get("nav", pd.Series(dtype=float)))
    if strategy_curve.empty:
        st.info("No strategy equity series available.")
        return

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=curve["_time"],
            y=strategy_curve.astype(float) - 1.0,
            mode="lines",
            name="Strategy",
        )
    )

    if "benchmark_equity" in curve.columns:
        benchmark_ret = curve["benchmark_equity"].astype(float) - 1.0
        if benchmark_ret.notna().any():
            fig.add_trace(
                go.Scatter(
                    x=curve["_time"],
                    y=benchmark_ret,
                    mode="lines",
                    name="Benchmark",
                    line=dict(dash="dot"),
                )
            )

    fig.update_layout(height=CHART_HEIGHT_RETURN, margin=dict(l=10, r=10, t=10, b=10), yaxis_tickformat=".1%", plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", xaxis=dict(gridcolor="#F1F5F9"), yaxis=dict(gridcolor="#F1F5F9"), legend=dict(orientation="h", yanchor="top", y=-0.2, xanchor="left", x=0))
    st.plotly_chart(fig, use_container_width=True)


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
        if data.signals.empty or "price" not in data.signals.columns:
            st.info("No signal/price data available for this selection.")
        else:
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=data.signals["_time"], y=data.signals["price"].astype(float), mode="lines", name="Raw Price"))

            if "signal_strength" in data.signals.columns:
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
            general_df = build_general_metrics_table(data.perf_raw)
            specific_df = build_strategy_specific_table(data.perf_raw)

            merged = pd.concat([
                general_df[["Metric", "Strategy", "Benchmark"]],
                specific_df.assign(Benchmark="-")[["Metric", "Value", "Benchmark"]].rename(columns={"Value": "Strategy"}),
            ], ignore_index=True)
            metric_order = ordered_metrics(merged["Metric"].astype(str).tolist())
            merged["Metric"] = pd.Categorical(merged["Metric"], categories=metric_order, ordered=True)
            merged = merged.sort_values("Metric", na_position="last").reset_index(drop=True)
            merged["Metric"] = merged["Metric"].astype(str)

            highlight_metrics = {"Total Return (Active Period)", "Max Drawdown (Active Period)", "Volatility (Active Period)", "Total Return (Full Period)", "Max Drawdown (Full Period)", "Volatility (Full Period)"}

            def _to_num(x: str) -> float | None:
                try:
                    return float(str(x).replace('%', '').strip())
                except Exception:
                    return None

            def _row_style(row: pd.Series):
                if str(row.get("Metric", "")) in highlight_metrics:
                    gv = _to_num(row.get("Strategy", ""))
                    bv = _to_num(row.get("Benchmark", ""))
                    if gv is None or bv is None:
                        return [""] * len(row)
                    good = (("Total Return" in row["Metric"]) and (gv > bv)) or (("Max Drawdown" in row["Metric"]) and (gv > bv)) or (("Volatility" in row["Metric"]) and (gv < bv))
                    if good:
                        return ["background-color: #DCFCE7"] * len(row)
                return [""] * len(row)

            st.dataframe(
                merged[["Metric", "Strategy", "Benchmark"]].style.apply(_row_style, axis=1),
                use_container_width=True,
                hide_index=True,
                height=TABLE_HEIGHT_PERF,
            )

            with st.popover("Metric Guide", use_container_width=True):
                defs = pd.DataFrame({"Metric": metric_order})
                defs["Definition"] = defs["Metric"].map(lambda x: METRIC_DEFINITIONS.get(x, "Definition pending."))
                st.dataframe(
                    defs[["Metric", "Definition"]],
                    use_container_width=True,
                    hide_index=True,
                    height=TABLE_HEIGHT_PERF,
                )

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
        order_df = build_order_details(data.signals)
        st.dataframe(order_df, use_container_width=True, hide_index=True, height=TABLE_HEIGHT_PERF)


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
        </style>
        """,
        unsafe_allow_html=True,
    )

    cfg = get_cfg()
    if not cfg.token:
        st.error("Missing INFLUX_TOKEN (or DOCKER_INFLUXDB_INIT_ADMIN_TOKEN).")
        st.stop()

    with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
        strategies = tag_values(
            client,
            cfg,
            "strategy_performance",
            "strategy",
            predicate=f'r.schema_version == "{SCHEMA_VERSION}"',
        )
        if not strategies:
            st.warning("No strategy_performance data found yet.")
            st.stop()

        st.sidebar.header("Filters")
        strategy = st.sidebar.selectbox("Strategy", strategies, index=0)

        contexts_preview = load_strategy_contexts()
        meta_preview = contexts_preview.get(strategy)
        if not isinstance(meta_preview, dict):
            st.error(f"strategy_context missing strategy: {strategy}")
            st.stop()
        try:
            validate_strategy_context_or_raise(meta_preview, strategy)
        except SchemaValidationError as e:
            st.error(f"Schema validation failed: {e}")
            st.stop()
        periods_preview = meta_preview.get("periods", {}) or {}

        sample = st.sidebar.selectbox(
            "Sample",
            SAMPLE_OPTIONS,
            index=0,
            format_func=lambda x: sample_label(x, periods_preview),
        )

        sample_predicate = "true" if sample == "full" else f'r.sample == "{sample}"'
        run_ids = tag_values(
            client,
            cfg,
            "strategy_performance",
            "run_id",
            predicate=f'r.schema_version == "{SCHEMA_VERSION}" and r.strategy == "{strategy}" and {sample_predicate}',
        )
        if not run_ids:
            st.warning("No run_id found for selected strategy/sample.")
            st.stop()
        run_id = st.sidebar.selectbox("Run ID", run_ids[::-1], index=0)

        try:
            data = build_dashboard_data(client, cfg, strategy, sample, run_id)
        except SchemaValidationError as e:
            st.error(f"Schema validation failed: {e}")
            st.stop()

    context = meta_context(data.meta)
    strategy_logic = str(data.meta.get("logic") or "No strategy logic provided.")
    alpha_value = "N/A"
    if not data.perf_raw.empty:
        perf_map = {str(r.get("_field")): float(r.get("_value", 0.0)) for _, r in data.perf_raw.iterrows()}
        strategy_ret = perf_map.get("total_return")
        benchmark_ret = perf_map.get("bh_total_return")
        if strategy_ret is not None and benchmark_ret is not None:
            alpha_value = f"{(strategy_ret - benchmark_ret):.2%}"

    tab_performance, tab_parameters = st.tabs(["Performance", "Parameter"])

    with tab_performance:
        render_performance_tab(data, context, alpha_value, strategy_logic)

    with tab_parameters:
        render_parameter_tab(data.meta)


if __name__ == "__main__":
    main()
