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
  |> filter(fn:(r)=> r._field == "signal_strength" or r._field == "price")
  |> pivot(rowKey:["_time","strategy","run_id","side"], columnKey:["_field"], valueColumn:"_value")
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
    st.set_page_config(page_title="策略回測分析", layout="wide")
    st.title("策略回測分析")
    st.caption("UI build: 2026-03-05-1036")

    section1_height = 580
    st.markdown(
        f"""
        <style>
        div[data-baseweb=\"tab-panel\"] {{ min-height: {section1_height}px; }}
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

        c1, c2, c3 = st.columns([4, 2, 3])
        strategy = c1.selectbox("Strategy", strategies, index=0)
        sample = c2.selectbox("Sample", ["oos", "train", "full"], index=0)
        run_ids = tag_values(client, cfg, "strategy_performance", "run_id")
        run_id = c3.selectbox("Run ID", run_ids[::-1], index=0 if run_ids else None)

        curve = load_curve(client, cfg, strategy, sample, run_id)
        signals = load_signals(client, cfg, strategy, run_id)
        trade_pnl = load_trade_pnl(client, cfg, strategy, run_id)
        perf_raw = load_perf(client, cfg, strategy, sample, run_id)

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

    # Section 1 (merged Decision + Context)
    with st.container(border=True):
        st.markdown("### 策略總覽")

        summary = meta.get("summary", {})
        periods = meta.get("periods", {})
        full_period = summary.get("full_sample_period", periods.get("full", "N/A"))
        train_period = summary.get("train_period", periods.get("train", "N/A"))
        oos_period = summary.get("oos_period", periods.get("oos", "N/A"))
        asset = summary.get("asset", ", ".join(meta.get("universe", [])) if meta.get("universe") else "N/A")
        freq = str(summary.get("freq", meta.get("params", {}).get("timeframe", "N/A")))

        h1, h2, h3, h4 = st.columns([2.4, 1.8, 1.4, 1.2])
        h1.metric("全樣本期間", full_period)
        h2.markdown(
            f"<div style='font-size:0.85rem;color:#9aa0a6;'>Train: {train_period}<br>測試期間: {oos_period}</div>",
            unsafe_allow_html=True,
        )
        h3.metric("交易標的", asset)
        h4.metric("交易頻率", freq)

        t_perf, t_risk, t_setup = st.tabs(["績效分析", "風險分析", "策略設定"])

        with t_perf:
            _left, _right = st.columns([8, 1])
            with _right:
                show_signals = st.toggle("顯示訊號", value=True, key="show_signals_main")

            main_chart_height = 340
            if curve.empty:
                st.warning("目前篩選條件下沒有可用的績效曲線資料。")
            else:
                curve["strategy_ret"] = (curve.get("equity", curve.get("nav", pd.Series(dtype=float))).astype(float) - 1.0)
                curve["benchmark_ret"] = curve["benchmark_equity"].astype(float) - 1.0 if "benchmark_equity" in curve.columns else pd.NA

                fig = go.Figure()
                fig.add_trace(go.Scatter(x=curve["_time"], y=curve["strategy_ret"], mode="lines", name="Strategy"))
                if curve["benchmark_ret"].notna().any():
                    fig.add_trace(go.Scatter(x=curve["_time"], y=curve["benchmark_ret"], mode="lines", name="Benchmark", line=dict(dash="dot")))

                if show_signals and not signals.empty and "signal_strength" in signals.columns:
                    m = pd.DataFrame()
                    m["_time"] = signals["_time"]
                    m["marker_y"] = signals["signal_strength"].apply(lambda v: 0.02 if float(v) > 0 else (-0.02 if float(v) < 0 else 0.0))
                    m["name"] = signals.get("side", pd.Series(["signal"] * len(signals))).astype(str)
                    fig.add_trace(go.Scatter(x=m["_time"], y=m["marker_y"], mode="markers", name="Signals", marker=dict(size=7, symbol="circle-open"), text=m["name"], hovertemplate="%{x}<br>Signal: %{text}<extra></extra>"))

                fig.update_layout(height=main_chart_height, margin=dict(l=10, r=10, t=10, b=10), yaxis_tickformat=".1%")
                st.plotly_chart(fig, use_container_width=True)

            st.markdown("**績效摘要表**")
            if perf_raw.empty:
                st.warning("目前篩選條件下沒有可用的績效資料。")
            else:
                table = build_perf_table(perf_raw)
                st.dataframe(table, use_container_width=True, hide_index=True, height=180)

        with t_risk:
            st.markdown("**最大回撤趨勢（MDD）**")
            if curve.empty or "drawdown" not in curve.columns:
                st.info("目前沒有可用的回撤序列資料。")
            else:
                mdd_fig = go.Figure()
                mdd_fig.add_trace(go.Scatter(x=curve["_time"], y=curve["drawdown"].astype(float), mode="lines", name="MDD"))
                mdd_fig.update_layout(height=360, margin=dict(l=10, r=10, t=10, b=10), yaxis_tickformat=".1%", showlegend=False)
                st.plotly_chart(mdd_fig, use_container_width=True)

        with t_setup:
            st.markdown("**策略邏輯**")
            st.write(meta.get("logic", "N/A"))

            assumptions = meta.get("assumptions", [])
            if assumptions:
                st.markdown("**交易假設**")
                for a in assumptions:
                    st.markdown(f"- {a}")

            st.markdown("**策略參數**")
            params = meta.get("params", {})
            if params:
                st.dataframe(pd.DataFrame([{"Parameter": k, "Value": v} for k, v in params.items()]), use_container_width=True, hide_index=True, height=200)

            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**成本模型**")
                cost_model = meta.get("cost_model", {})
                if cost_model:
                    st.dataframe(pd.DataFrame([{"Key": k, "Value": v} for k, v in cost_model.items()]), use_container_width=True, hide_index=True, height=180)
            with c2:
                st.markdown("**風險限制**")
                risk_limits = meta.get("risk_limits", {})
                if risk_limits:
                    st.dataframe(pd.DataFrame([{"Key": k, "Value": v} for k, v in risk_limits.items()]), use_container_width=True, hide_index=True, height=180)

    # Section 3
    with st.container(border=True):
        st.markdown("### 交易明細")
        col_pnl, col_tbl = st.columns([4, 6])

        with col_pnl:
            st.markdown("**逐筆損益（不含成本）**")
            if trade_pnl.empty:
                st.info("目前沒有可用的逐筆損益資料。")
            else:
                pnl_fig = go.Figure()
                pnl_fig.add_trace(go.Scatter(x=trade_pnl["_time"], y=trade_pnl["_value"].astype(float), mode="markers", marker=dict(size=8), name="Trade PnL"))
                pnl_fig.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10), yaxis_tickformat=".1%", showlegend=False)
                st.plotly_chart(pnl_fig, use_container_width=True)

        with col_tbl:
            st.markdown("**訂單明細**")
            if signals.empty:
                st.info("目前沒有可用的訂單/訊號明細資料。")
            else:
                od = pd.DataFrame()
                od["時間"] = pd.to_datetime(signals["_time"], utc=True)
                od["raw_price"] = signals["price"] if "price" in signals.columns else pd.NA
                side_map = {"buy": "buy", "sell": "sell", "short": "sell_short", "sell_short": "sell_short", "cover": "buy_to_cover", "buy_to_cover": "buy_to_cover"}
                od["倉位"] = signals.get("side", pd.Series(["unknown"] * len(signals))).astype(str).str.lower().map(side_map).fillna(signals.get("side", pd.Series(["unknown"] * len(signals))).astype(str))

                if not trade_pnl.empty:
                    pnl = trade_pnl[["_time", "_value"]].copy().rename(columns={"_time": "時間", "_value": "該筆交易損益（不含成本）"})
                    pnl["時間"] = pd.to_datetime(pnl["時間"], utc=True)
                    od = pd.merge_asof(od.sort_values("時間"), pnl.sort_values("時間"), on="時間", direction="nearest", tolerance=pd.Timedelta("2h"))
                else:
                    od["該筆交易損益（不含成本）"] = pd.NA

                od = od[["時間", "倉位", "raw_price", "該筆交易損益（不含成本）"]].sort_values("時間", ascending=False)
                st.dataframe(od, use_container_width=True, hide_index=True, height=280)


if __name__ == "__main__":
    main()
