from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from influxdb_client import InfluxDBClient


APP_TITLE = "Strategy Backtest Analysis"
APP_CAPTION = "UI build: 2026-03-06"

TABLE_HEIGHT_PERF = 260
TABLE_HEIGHT_PARAM = 280
CHART_HEIGHT_PRICE = 300
CHART_HEIGHT_RETURN = 230

SAMPLE_OPTIONS = ["oos", "train", "full"]

DEFAULT_META = {
    "periods": {"full": "N/A", "train": "N/A", "oos": "N/A"},
    "assumptions": [],
    "logic": "N/A",
    "params": {},
    "benchmark": "N/A",
    "data_source": "N/A",
    "data_version": "N/A",
    "last_updated_utc": "N/A",
    "summary": {},
    "session_rules": {},
    "cost_model": {},
    "risk_limits": {},
    "universe": [],
}

PERF_METRIC_MAP = {
    "total_return": ("Total Return", "Strategy"),
    "bh_total_return": ("Total Return", "Benchmark"),
    "max_drawdown": ("Max Drawdown", "Strategy"),
    "profit_factor": ("Profit Factor", "Strategy"),
    "win_rate": ("Win Rate", "Strategy"),
    "avg_trade_return": ("Avg Trade Return", "Strategy"),
    "trades": ("Trades", "Strategy"),
    "exposure_ratio": ("Exposure Ratio", "Strategy"),
}

PERF_METRIC_ORDER = [
    "Total Return",
    "Max Drawdown",
    "Profit Factor",
    "Win Rate",
    "Avg Trade Return",
    "Trades",
    "Exposure Ratio",
]


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


def tag_values(client: InfluxDBClient, cfg: InfluxCfg, measurement: str, tag: str) -> list[str]:
    flux = f'''
import "influxdata/influxdb/schema"
schema.tagValues(bucket: "{cfg.bucket}", tag: "{tag}", predicate: (r) => r._measurement == "{measurement}", start: -365d)
'''
    df = query_df(client, cfg.org, flux)
    if df.empty or "_value" not in df.columns:
        return []
    return sorted([str(v) for v in df["_value"].dropna().unique()])


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


def load_curve(client: InfluxDBClient, cfg: InfluxCfg, strategy: str, sample: str, run_id: str) -> pd.DataFrame:
    flux = f'''
from(bucket: "{cfg.bucket}")
  |> range(start: -365d)
  |> filter(fn:(r)=> r._measurement=="perf_equity_curve")
  |> filter(fn:(r)=> r.strategy == "{strategy}")
  |> filter(fn:(r)=> {_sample_filter(sample)})
  |> filter(fn:(r)=> r.run_id == "{run_id}")
  |> filter(fn:(r)=> r._field == "equity" or r._field == "benchmark_equity" or r._field == "drawdown")
  |> pivot(rowKey:["_time","strategy","sample","run_id"], columnKey:["_field"], valueColumn:"_value")
  |> sort(columns:["_time"], desc:false)
'''
    df = query_df(client, cfg.org, flux)
    if df.empty:
        flux2 = f'''
from(bucket: "{cfg.bucket}")
  |> range(start: -365d)
  |> filter(fn:(r)=> r._measurement=="perf_equity_curve")
  |> filter(fn:(r)=> r.strategy == "{strategy}")
  |> filter(fn:(r)=> {_sample_filter(sample)})
  |> filter(fn:(r)=> r.run_id == "{run_id}")
  |> filter(fn:(r)=> r._field == "nav" or r._field == "drawdown")
  |> keep(columns:["_time","_field","_value","curve_type"])
'''
        raw = query_df(client, cfg.org, flux2)
        if raw.empty:
            return raw
        raw["_time"] = pd.to_datetime(raw["_time"], utc=True)
        nav = raw[raw["_field"] == "nav"].pivot_table(index="_time", columns="curve_type", values="_value", aggfunc="last")
        dd = raw[raw["_field"] == "drawdown"].groupby("_time")["_value"].last()
        out = nav.reset_index()
        if "strategy" in out.columns:
            out = out.rename(columns={"strategy": "equity"})
        if "buyhold" in out.columns:
            out = out.rename(columns={"buyhold": "benchmark_equity"})
        out = out.merge(dd.rename("drawdown"), on="_time", how="left")
        return out

    df["_time"] = pd.to_datetime(df["_time"], utc=True)
    return df


def load_signals(client: InfluxDBClient, cfg: InfluxCfg, strategy: str, run_id: str) -> pd.DataFrame:
    flux = f'''
from(bucket: "{cfg.bucket}")
  |> range(start: -365d)
  |> filter(fn:(r)=> r._measurement=="strategy_signals")
  |> filter(fn:(r)=> r.strategy == "{strategy}")
  |> filter(fn:(r)=> r.run_id == "{run_id}")
  |> filter(fn:(r)=> r._field == "signal_strength" or r._field == "price")
  |> pivot(rowKey:["_time","strategy","run_id","side"], columnKey:["_field"], valueColumn:"_value")
  |> sort(columns:["_time"], desc:false)
'''
    df = query_df(client, cfg.org, flux)
    if not df.empty:
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
    return df


def load_perf(client: InfluxDBClient, cfg: InfluxCfg, strategy: str, sample: str, run_id: str) -> pd.DataFrame:
    flux = f'''
from(bucket: "{cfg.bucket}")
  |> range(start: -365d)
  |> filter(fn:(r)=> r._measurement=="strategy_performance")
  |> filter(fn:(r)=> r.strategy == "{strategy}")
  |> filter(fn:(r)=> {_sample_filter(sample)})
  |> filter(fn:(r)=> r.run_id == "{run_id}")
  |> filter(fn:(r)=> r._field == "total_return" or r._field == "max_drawdown" or r._field == "profit_factor" or r._field == "win_rate" or r._field == "avg_trade_return" or r._field == "trades" or r._field == "exposure_ratio" or r._field == "bh_total_return")
  |> last()
'''
    return query_df(client, cfg.org, flux)


def build_perf_table(perf_raw: pd.DataFrame) -> pd.DataFrame:
    rows: dict[str, dict[str, float | str | None]] = {}
    for _, r in perf_raw.iterrows():
        field = str(r.get("_field"))
        if field not in PERF_METRIC_MAP:
            continue
        metric_name, column_name = PERF_METRIC_MAP[field]
        value = float(r.get("_value", 0.0))
        rows.setdefault(metric_name, {"Metric": metric_name, "Strategy": None, "Benchmark": None})
        rows[metric_name][column_name] = value

    table = pd.DataFrame(list(rows.values()))
    if table.empty:
        return table

    table["Metric"] = pd.Categorical(table["Metric"], categories=PERF_METRIC_ORDER, ordered=True)
    return table.sort_values("Metric").reset_index(drop=True)


def flatten_dict(prefix: str, data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(flatten_dict(full_key, value))
        else:
            out[full_key] = value
    return out


def kv_to_df(kv: dict[str, Any]) -> pd.DataFrame:
    rows = [{"Key": str(k), "Value": ", ".join(map(str, v)) if isinstance(v, list) else v} for k, v in kv.items()]
    return pd.DataFrame(rows)


def build_order_details(signals: pd.DataFrame) -> pd.DataFrame:
    if signals.empty:
        return pd.DataFrame(columns=["Time", "Position", "Execution Price", "Gross PnL"])

    out = pd.DataFrame()
    out["Time"] = pd.to_datetime(signals.get("_time", pd.Series([], dtype="datetime64[ns]")), utc=True, errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    out["Position"] = signals.get("side", pd.Series(["N/A"] * len(signals))).astype(str)
    out["Execution Price"] = pd.to_numeric(signals.get("price", pd.Series([None] * len(signals))), errors="coerce")
    out["Gross PnL"] = "N/A"
    out = out.dropna(subset=["Time"]).sort_values("Time", ascending=False).reset_index(drop=True)
    return out


def render_kv_table(title: str, kv: dict[str, Any], height: int = TABLE_HEIGHT_PARAM) -> None:
    with st.container(border=True):
        st.markdown(f"**{title}**")
        if not kv:
            st.info("No data available.")
            return
        st.dataframe(kv_to_df(kv), use_container_width=True, hide_index=True, height=height)


def meta_context(meta: dict[str, Any]) -> dict[str, str]:
    summary = meta.get("summary", {}) or {}
    periods = meta.get("periods", {}) or {}
    return {
        "Date Range (Full)": str(summary.get("full_sample_period", periods.get("full", "N/A"))),
        "Date Range (Train)": str(summary.get("train_period", periods.get("train", "N/A"))),
        "Date Range (OOS)": str(summary.get("oos_period", periods.get("oos", "N/A"))),
        "Benchmark": str(meta.get("benchmark", "N/A")),
        "Asset": str(summary.get("asset", ", ".join(meta.get("universe", [])) if meta.get("universe") else "N/A")),
        "Frequency": str(summary.get("freq", meta.get("params", {}).get("timeframe", "N/A"))),
    }


def build_dashboard_data(client: InfluxDBClient, cfg: InfluxCfg, strategy: str, sample: str, run_id: str) -> DashboardData:
    curve = load_curve(client, cfg, strategy, sample, run_id)
    signals = load_signals(client, cfg, strategy, run_id)
    perf_raw = load_perf(client, cfg, strategy, sample, run_id)

    contexts = load_strategy_contexts()
    meta = {**DEFAULT_META, **contexts.get(strategy, {})}
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
                    marker=dict(symbol="triangle-up", size=10),
                )
            )
        if sell_mask.any():
            fig.add_trace(
                go.Scatter(
                    x=signals.loc[sell_mask, "_time"],
                    y=signals.loc[sell_mask, "price"].astype(float),
                    mode="markers",
                    name="Sell Signal",
                    marker=dict(symbol="triangle-down", size=10),
                )
            )

    fig.update_layout(height=CHART_HEIGHT_PRICE, margin=dict(l=10, r=10, t=10, b=10))
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

    fig.update_layout(height=CHART_HEIGHT_RETURN, margin=dict(l=10, r=10, t=10, b=10), yaxis_tickformat=".1%")
    st.plotly_chart(fig, use_container_width=True)


def render_performance_tab(data: DashboardData, overview_ctx: dict[str, str], alpha_value: str) -> None:
    st.markdown("### Strategy Overview")
    st.markdown("<div style='font-size:1.25rem;'>Trend-following breakout strategy: enter long when price breaks above the 20-bar high with momentum confirmation, exit on trailing-stop or momentum reversal.</div>", unsafe_allow_html=True)

    full_period = overview_ctx.get("Date Range (Full)", "N/A")
    if "~" in full_period:
        start, end = [x.strip() for x in full_period.split("~", 1)]
        full_period_display = f"{start}\n{end}"
    else:
        full_period_display = full_period.replace(" to ", "\n")

    c1, c2, c3, c4 = st.columns(4)
    with c1.container(border=True):
        st.markdown("**Full Period**")
        st.markdown(full_period_display.replace("\n", "  \n"))
    c2.metric("Asset", overview_ctx.get("Asset", "N/A"))
    c3.metric("Frequency", overview_ctx.get("Frequency", "N/A"))
    c4.metric("Alpha", alpha_value or "N/A")

    top_left, top_right = st.columns(2)
    with top_left:
        st.markdown("#### Asset Price with Trading Signals")
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
                        marker=dict(symbol="triangle-up", size=10),
                    ))
                if sell_mask.any():
                    fig.add_trace(go.Scatter(
                        x=data.signals.loc[sell_mask, "_time"],
                        y=data.signals.loc[sell_mask, "price"].astype(float),
                        mode="markers",
                        name="Sell Signal",
                        marker=dict(symbol="triangle-down", size=10),
                    ))

            fig.update_layout(height=CHART_HEIGHT_PRICE, margin=dict(l=10, r=10, t=10, b=10))
            st.plotly_chart(fig, use_container_width=True)

    with top_right:
        st.markdown("#### Performance Analysis")
        if data.perf_raw.empty:
            st.info("No performance metrics available for this selection.")
        else:
            table = build_perf_table(data.perf_raw)
            st.dataframe(table, use_container_width=True, hide_index=True, height=TABLE_HEIGHT_PERF)

    bottom_left, bottom_right = st.columns(2)
    with bottom_left:
        st.markdown("#### Cumulative Return: Strategy vs. Buy and Hold")
        if data.curve.empty:
            st.info("No equity curve data available for this selection.")
        else:
            strategy_curve = data.curve.get("equity", data.curve.get("nav", pd.Series(dtype=float)))
            if strategy_curve.empty:
                st.info("No strategy equity series available.")
            else:
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=data.curve["_time"], y=strategy_curve.astype(float) - 1.0, mode="lines", name="Strategy"))
                if "benchmark_equity" in data.curve.columns:
                    benchmark_ret = data.curve["benchmark_equity"].astype(float) - 1.0
                    if benchmark_ret.notna().any():
                        fig.add_trace(go.Scatter(
                            x=data.curve["_time"],
                            y=benchmark_ret,
                            mode="lines",
                            name="Buy and Hold",
                            line=dict(dash="dot"),
                        ))
                fig.update_layout(height=CHART_HEIGHT_RETURN, margin=dict(l=10, r=10, t=10, b=10), yaxis_tickformat=".1%")
                st.plotly_chart(fig, use_container_width=True)

    with bottom_right:
        st.markdown("#### Order Details")
        order_df = build_order_details(data.signals)
        st.dataframe(order_df, use_container_width=True, hide_index=True, height=TABLE_HEIGHT_PERF)


def render_parameter_tab(meta: dict[str, Any]) -> None:
    data_card = {
        "benchmark": meta.get("benchmark", "N/A"),
        "data_source": meta.get("data_source", "N/A"),
        "data_version": meta.get("data_version", "N/A"),
        "last_updated_utc": meta.get("last_updated_utc", "N/A"),
    }
    trading_card = {
        **flatten_dict("session_rules", meta.get("session_rules", {}) or {}),
        **flatten_dict("cost_model", meta.get("cost_model", {}) or {}),
    }
    assumptions = meta.get("assumptions", []) or []
    if assumptions:
        trading_card["assumptions"] = assumptions

    risk_card = flatten_dict("risk_limits", meta.get("risk_limits", {}) or {})

    strategy_card = {
        "logic": meta.get("logic", "N/A"),
        **flatten_dict("params", meta.get("params", {}) or {}),
    }

    row1_col1, row1_col2 = st.columns(2)
    with row1_col1:
        render_kv_table("Data", data_card)
    with row1_col2:
        render_kv_table("Trading", trading_card)

    row2_col1, row2_col2 = st.columns(2)
    with row2_col1:
        render_kv_table("Risk", risk_card)
    with row2_col2:
        render_kv_table("Strategy", strategy_card)


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)
    st.caption(APP_CAPTION)

    cfg = get_cfg()
    if not cfg.token:
        st.error("Missing INFLUX_TOKEN (or DOCKER_INFLUXDB_INIT_ADMIN_TOKEN).")
        st.stop()

    with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
        strategies = tag_values(client, cfg, "strategy_performance", "strategy")
        if not strategies:
            st.warning("No strategy_performance data found yet.")
            st.stop()

        st.sidebar.header("Filters")
        strategy = st.sidebar.selectbox("Strategy", strategies, index=0)
        sample = st.sidebar.selectbox("Sample", SAMPLE_OPTIONS, index=0)
        run_ids = tag_values(client, cfg, "strategy_performance", "run_id")
        run_id = st.sidebar.selectbox("Run ID", run_ids[::-1], index=0 if run_ids else None)

        data = build_dashboard_data(client, cfg, strategy, sample, run_id)

    context = meta_context(data.meta)
    alpha_value = str((data.meta.get("summary", {}) or {}).get("alpha", "20%"))

    tab_performance, tab_parameters = st.tabs(["Performance", "Parameter"])

    with tab_performance:
        render_performance_tab(data, context, alpha_value)

    with tab_parameters:
        render_parameter_tab(data.meta)


if __name__ == "__main__":
    main()
