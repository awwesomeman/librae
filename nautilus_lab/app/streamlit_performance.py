from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from influxdb_client import InfluxDBClient


@dataclass
class InfluxCfg:
    url: str
    org: str
    bucket: str
    token: str


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


def load_strategy_contexts() -> dict:
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
  |> filter(fn:(r)=> r._field == "signal_strength")
  |> keep(columns:["_time","_value","side"])
  |> sort(columns:["_time"], desc:false)
'''
    df = query_df(client, cfg.org, flux)
    if not df.empty:
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
    return df


def load_trade_pnl(client: InfluxDBClient, cfg: InfluxCfg, strategy: str, run_id: str) -> pd.DataFrame:
    flux = f'''
from(bucket: "{cfg.bucket}")
  |> range(start: -365d)
  |> filter(fn:(r)=> r._measurement=="trade_pnl")
  |> filter(fn:(r)=> r.strategy == "{strategy}")
  |> filter(fn:(r)=> r.run_id == "{run_id}")
  |> filter(fn:(r)=> r._field == "trade_pnl_pct")
  |> keep(columns:["_time","_value"])
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
    strategy_map = {
        "total_return": "Total Return",
        "max_drawdown": "Max Drawdown",
        "profit_factor": "Profit Factor",
        "win_rate": "Win Rate",
        "avg_trade_return": "Avg Trade Return",
        "trades": "#Trades",
        "exposure_ratio": "Exposure Ratio",
    }
    benchmark_map = {
        "bh_total_return": "Total Return",
    }

    rows: dict[str, dict[str, float | str | None]] = {}
    for _, r in perf_raw.iterrows():
        fld = str(r.get("_field"))
        val = float(r.get("_value", 0.0))
        if fld in strategy_map:
            m = strategy_map[fld]
            rows.setdefault(m, {"Metric": m, "Strategy": None, "Benchmark": None})
            rows[m]["Strategy"] = val
        elif fld in benchmark_map:
            m = benchmark_map[fld]
            rows.setdefault(m, {"Metric": m, "Strategy": None, "Benchmark": None})
            rows[m]["Benchmark"] = val

    table = pd.DataFrame(list(rows.values()))
    order = [
        "Total Return",
        "Max Drawdown",
        "Profit Factor",
        "Win Rate",
        "Avg Trade Return",
        "#Trades",
        "Exposure Ratio",
    ]
    if not table.empty:
        table["Metric"] = pd.Categorical(table["Metric"], categories=order, ordered=True)
        table = table.sort_values("Metric")
    return table


def main() -> None:
    st.set_page_config(page_title="Nautilus Performance (Final)", layout="wide")
    st.title("Nautilus Performance (Final)")
    st.caption("Cumulative Return + Performance Analysis")

    cfg = get_cfg()
    if not cfg.token:
        st.error("Missing INFLUX_TOKEN (or DOCKER_INFLUXDB_INIT_ADMIN_TOKEN).")
        st.stop()

    with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
        strategies = tag_values(client, cfg, "strategy_performance", "strategy")
        if not strategies:
            st.warning("No strategy_performance data found yet.")
            st.stop()

        c1, c2, c3, c4 = st.columns([3, 2, 3, 2])
        strategy = c1.selectbox("Strategy", strategies, index=0)
        sample = c2.selectbox("Sample", ["oos", "train", "full"], index=0)
        run_ids = tag_values(client, cfg, "strategy_performance", "run_id")
        run_id = c3.selectbox("Run ID", run_ids[::-1], index=0 if run_ids else None)
        show_signals = c4.toggle("Show Signals", value=True)

        strategy_meta = load_strategy_contexts()
        meta = strategy_meta.get(
            strategy,
            {
                "periods": {"full": "N/A", "train": "N/A", "oos": "N/A"},
                "assumptions": ["Commission: N/A", "Slippage: N/A", "Execution: N/A", "Timezone: UTC"],
                "logic": "N/A",
                "params": {},
                "benchmark": "N/A",
                "data_source": "N/A",
            },
        )

        with st.container(border=True):
            st.markdown("### Strategy & Backtest Context")
            p1, p2, p3 = st.columns([1.1, 1.2, 2.2])
            with p1:
                st.markdown("**Periods**")
                st.markdown(f"- Full: `{meta.get('periods', {}).get('full','N/A')}`")
                st.markdown(f"- Train: `{meta.get('periods', {}).get('train','N/A')}`")
                st.markdown(f"- OOS: `{meta.get('periods', {}).get('oos','N/A')}`")
                st.markdown(f"- Benchmark: `{meta.get('benchmark', 'N/A')}`")
                st.markdown(f"- Data Source: `{meta.get('data_source', 'N/A')}`")
            with p2:
                st.markdown("**Trading Assumptions**")
                for a in meta.get("assumptions", []):
                    st.markdown(f"- {a}")
            with p3:
                st.markdown("**Logic & Parameters**")
                st.markdown(f"- Logic: {meta.get('logic','N/A')}")
                if meta.get("params"):
                    pm = pd.DataFrame([{"Parameter": k, "Value": v} for k, v in meta["params"].items()])
                    st.dataframe(pm, use_container_width=True, hide_index=True)
                else:
                    st.caption("No parameter metadata")

        curve = load_curve(client, cfg, strategy, sample, run_id)
        signals = load_signals(client, cfg, strategy, run_id) if show_signals else pd.DataFrame()
        trade_pnl = load_trade_pnl(client, cfg, strategy, run_id)
        perf_raw = load_perf(client, cfg, strategy, sample, run_id)

    left, right = st.columns([2.0, 1.4])

    with left:
        if curve.empty:
            st.warning("No curve data for selected filters.")
        else:
            curve["strategy_ret"] = (curve.get("equity", curve.get("nav", pd.Series(dtype=float))).astype(float) - 1.0)
            if "benchmark_equity" in curve.columns:
                curve["benchmark_ret"] = curve["benchmark_equity"].astype(float) - 1.0
            else:
                curve["benchmark_ret"] = pd.NA

            fig = go.Figure()
            fig.add_trace(go.Scatter(x=curve["_time"], y=curve["strategy_ret"], mode="lines", name="Strategy"))
            if curve["benchmark_ret"].notna().any():
                fig.add_trace(go.Scatter(x=curve["_time"], y=curve["benchmark_ret"], mode="lines", name="Benchmark", line=dict(dash="dot")))

            if show_signals and not signals.empty:
                m = pd.DataFrame()
                m["_time"] = signals["_time"]
                m["marker_y"] = signals["_value"].apply(lambda v: 0.02 if float(v) > 0 else (-0.02 if float(v) < 0 else 0.0))
                m["name"] = signals.get("side", pd.Series(["signal"] * len(signals))).astype(str)
                fig.add_trace(
                    go.Scatter(
                        x=m["_time"],
                        y=m["marker_y"],
                        mode="markers",
                        name="Signals",
                        marker=dict(size=7, symbol="circle-open"),
                        text=m["name"],
                        hovertemplate="%{x}<br>Signal: %{text}<extra></extra>",
                    )
                )

            fig.update_layout(height=500, margin=dict(l=10, r=10, t=10, b=10), yaxis_tickformat=".1%")
            st.plotly_chart(fig, use_container_width=True)

    with right:
        st.subheader("MDD Trend")
        if curve.empty or "drawdown" not in curve.columns:
            st.info("No drawdown series")
        else:
            mdd_fig = go.Figure()
            mdd_fig.add_trace(go.Scatter(x=curve["_time"], y=curve["drawdown"].astype(float), mode="lines", name="MDD"))
            mdd_fig.update_layout(height=240, margin=dict(l=10, r=10, t=10, b=10), yaxis_tickformat=".1%", showlegend=False)
            st.plotly_chart(mdd_fig, use_container_width=True)

        st.subheader("Profit and Loss")
        if trade_pnl.empty:
            st.info("No trade_pnl data")
        else:
            pnl_fig = go.Figure()
            pnl_fig.add_trace(
                go.Scatter(
                    x=trade_pnl["_time"],
                    y=trade_pnl["_value"].astype(float),
                    mode="markers",
                    marker=dict(size=8),
                    name="Trade PnL",
                )
            )
            pnl_fig.update_layout(height=240, margin=dict(l=10, r=10, t=10, b=10), yaxis_tickformat=".1%", showlegend=False)
            st.plotly_chart(pnl_fig, use_container_width=True)

    st.subheader("Performance Analysis")
    if perf_raw.empty:
        st.warning("No performance data for selected filters.")
        return

    table = build_perf_table(perf_raw)
    st.dataframe(table, use_container_width=True, hide_index=True)

    st.subheader("Order Detail")
    if signals.empty:
        st.info("No order/signal detail data")
    else:
        od = signals[["_time", "side", "_value"]].copy()
        od = od.rename(columns={"_time": "Time", "side": "Side", "_value": "Signal Strength"})
        od["Time"] = pd.to_datetime(od["Time"], utc=True)

        if not trade_pnl.empty:
            pnl = trade_pnl[["_time", "_value"]].copy()
            pnl = pnl.rename(columns={"_time": "Time", "_value": "Trade PnL %"})
            pnl["Time"] = pd.to_datetime(pnl["Time"], utc=True)
            od = od.sort_values("Time")
            pnl = pnl.sort_values("Time")
            od = pd.merge_asof(od, pnl, on="Time", direction="nearest", tolerance=pd.Timedelta("2h"))
        else:
            od["Trade PnL %"] = pd.NA

        od = od.sort_values("Time", ascending=False)
        st.dataframe(od, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
