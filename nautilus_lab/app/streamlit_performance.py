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
APP_CAPTION = "UI build: 2026-03-06-0906"

TABLE_HEIGHT_PERF = 260
TABLE_HEIGHT_PARAM = 280
CHART_HEIGHT_PRICE = 300
CHART_HEIGHT_RETURN = 230

SAMPLE_OPTIONS = ["oos", "train", "full"]


def sample_label(sample: str, periods: dict[str, Any]) -> str:
    display = "test" if sample == "oos" else sample
    period_key = "oos" if sample == "oos" else sample
    period = str((periods or {}).get(period_key, "")).strip()
    return f"{display} ({period})" if period else display

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
    "avg_trade_return": ("Avg Return Per Trade", "Strategy"),
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

    s_total_active = pmap.get("active_total_return", pmap.get("total_return"))
    b_total_active = pmap.get("bh_active_total_return", pmap.get("bh_total_return"))
    s_mdd_active = pmap.get("active_max_drawdown", pmap.get("max_drawdown"))
    b_mdd_active = pmap.get("bh_active_max_drawdown", pmap.get("bh_max_drawdown"))
    s_vol_active = pmap.get("active_volatility", pmap.get("volatility"))
    b_vol_active = pmap.get("bh_active_volatility", pmap.get("bh_volatility"))

    s_total_full = pmap.get("total_return_full", pmap.get("total_return"))
    b_total_full = pmap.get("bh_total_return_full", pmap.get("bh_total_return"))
    s_mdd_full = pmap.get("max_drawdown_full", pmap.get("max_drawdown"))
    b_mdd_full = pmap.get("bh_max_drawdown_full", pmap.get("bh_max_drawdown"))
    s_vol_full = pmap.get("volatility_full", pmap.get("volatility"))
    b_vol_full = pmap.get("bh_volatility_full", pmap.get("bh_volatility"))

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
        return pd.DataFrame(columns=["TradeId", "Time", "Position", "ExecutionPrice", "GrossPnl"])

    out = pd.DataFrame()
    out["Time"] = pd.to_datetime(signals.get("_time", pd.Series([], dtype="datetime64[ns]")), utc=True, errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    side = signals.get("side", pd.Series(["buy"] * len(signals))).astype(str).str.lower().str.replace("_", " ")
    allowed = {"buy", "sell", "buy to cover", "sell short"}
    side = side.apply(lambda x: x if x in allowed else "buy")
    out["Position"] = side
    out["ExecutionPrice"] = pd.to_numeric(signals.get("price", pd.Series([None] * len(signals))), errors="coerce")
    out["GrossPnl"] = "N/A"

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
    out["TradeId"] = tids
    out = out.dropna(subset=["Time"]).sort_values("Time", ascending=False).reset_index(drop=True)
    return out[["TradeId", "Time", "Position", "ExecutionPrice", "GrossPnl"]]


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
    params = meta.get("params", {}) or {}

    signal_tf = str(params.get("signal_timeframe", params.get("timeframe", summary.get("freq", "N/A"))))
    exec_tf = str(params.get("execution_timeframe", params.get("entry_timeframe", signal_tf)))
    freq_display = f"Signal:{signal_tf} | Execution:{exec_tf}" if signal_tf != exec_tf else signal_tf

    return {
        "Date Range (Full)": str(summary.get("full_sample_period", periods.get("full", "N/A"))),
        "Date Range (Train)": str(summary.get("train_period", periods.get("train", "N/A"))),
        "Date Range (Test)": str(summary.get("test_period", summary.get("oos_period", periods.get("test", periods.get("oos", "N/A"))))),
        "Benchmark": str(meta.get("benchmark", "N/A")),
        "Asset": str(summary.get("asset", ", ".join(meta.get("universe", [])) if meta.get("universe") else "N/A")),
        "Frequency": str(summary.get("freq", params.get("timeframe", "N/A"))),
        "FrequencyDisplay": freq_display,
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

    fig.update_layout(height=CHART_HEIGHT_PRICE, margin=dict(l=10, r=10, t=10, b=10), plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", xaxis=dict(gridcolor="#F1F5F9"), yaxis=dict(gridcolor="#F1F5F9"))
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

    fig.update_layout(height=CHART_HEIGHT_RETURN, margin=dict(l=10, r=10, t=10, b=10), yaxis_tickformat=".1%", plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", xaxis=dict(gridcolor="#F1F5F9"), yaxis=dict(gridcolor="#F1F5F9"))
    st.plotly_chart(fig, use_container_width=True)


def render_performance_tab(data: DashboardData, overview_ctx: dict[str, str], alpha_value: str) -> None:
    st.markdown("### Strategy Overview")
    st.markdown(
        "<div class='overview-desc'>"
        "Trend-following breakout strategy: enter long when price breaks above the 20-bar high with momentum confirmation, "
        "exit on trailing-stop or momentum reversal."
        "</div>",
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

            fig.update_layout(height=CHART_HEIGHT_PRICE, margin=dict(l=10, r=10, t=10, b=10), plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", xaxis=dict(gridcolor="#F1F5F9"), yaxis=dict(gridcolor="#F1F5F9"))
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


            highlight_metrics = {"Total Return (Active Period)", "Max Drawdown (Active Period)", "Volatility (Active Period)"}

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
                    good = ("Total Return" in row["Metric"] and gv > bv) or ("Max Drawdown" in row["Metric"] and gv > bv) or ("Volatility" in row["Metric"] and gv < bv)
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
                defs = pd.DataFrame({
                    "Metric": [m for m in PERF_METRIC_ORDER if m in set(merged["Metric"].astype(str).tolist())],
                })
                defs["Definition"] = defs["Metric"].map(lambda x: METRIC_DEFINITIONS.get(x, "Definition pending."))
                defs["Formula"] = defs["Metric"].map(lambda x: METRIC_FORMULAS.get(x, "Formula pending."))
                st.dataframe(
                    defs,
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
                fig.update_layout(height=CHART_HEIGHT_RETURN, margin=dict(l=10, r=10, t=10, b=10), yaxis_tickformat=".1%", plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", xaxis=dict(gridcolor="#F1F5F9"), yaxis=dict(gridcolor="#F1F5F9"))
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
        strategies = tag_values(client, cfg, "strategy_performance", "strategy")
        if not strategies:
            st.warning("No strategy_performance data found yet.")
            st.stop()

        st.sidebar.header("Filters")
        strategy = st.sidebar.selectbox("Strategy", strategies, index=0)

        contexts_preview = load_strategy_contexts()
        meta_preview = {**DEFAULT_META, **contexts_preview.get(strategy, {})}
        periods_preview = meta_preview.get("periods", {}) or {}

        sample = st.sidebar.selectbox(
            "Sample",
            SAMPLE_OPTIONS,
            index=0,
            format_func=lambda x: sample_label(x, periods_preview),
        )

        run_ids = tag_values(client, cfg, "strategy_performance", "run_id")
        run_id = st.sidebar.selectbox("Run ID", run_ids[::-1], index=0 if run_ids else None)

        data = build_dashboard_data(client, cfg, strategy, sample, run_id)

    context = meta_context(data.meta)
    alpha_value = str((data.meta.get("summary", {}) or {}).get("alpha", "20%"))
    if not data.perf_raw.empty:
        perf_map = {str(r.get("_field")): float(r.get("_value", 0.0)) for _, r in data.perf_raw.iterrows()}
        strategy_ret = perf_map.get("active_total_return", perf_map.get("total_return"))
        benchmark_ret = perf_map.get("bh_active_total_return", perf_map.get("bh_total_return"))
        if strategy_ret is not None and benchmark_ret is not None:
            alpha_value = f"{(strategy_ret - benchmark_ret):.2%}"

    tab_performance, tab_parameters = st.tabs(["Performance", "Parameter"])

    with tab_performance:
        render_performance_tab(data, context, alpha_value)

    with tab_parameters:
        render_parameter_tab(data.meta)


if __name__ == "__main__":
    main()
