#!/usr/bin/env python3
"""Run TrendPullback backtest via Lumibot engine on real Binance BTC/USDT data.

Skills: python, quant

Pipeline:
  1. Fetch BTC/USDT 1m OHLCV from Binance (paginated)
  2. Resample to H1/D1, compute TrendPullback features
  3. Run through Lumibot backtesting engine with actual trading
  4. Build BacktestOutput with canonical metrics (metrics.py)
  5. Write canonical measurements to InfluxDB

Usage:
    python nautilus_lab/scripts/run_backtest_lumibot_btc.py [--months 1] [--dry-run] [--no-influx]
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lumibot.backtesting import PandasDataBacktesting
from lumibot.entities import Asset, Data
from lumibot.strategies import Strategy

from nautilus_lab.backtest.adapter import generate_run_id
from nautilus_lab.backtest.persistence import save_backtest_output
from nautilus_lab.backtest.schema import (
    BacktestOutput,
    EquityCurvePoint,
    RunMetadata,
    StrategyMetrics,
    TradeRecord,
)
from nautilus_lab.data.binance_fetcher import fetch_ohlcv
from nautilus_lab.monitoring.influx_writer import points_from_backtest
from scripts.etl.core_features import (
    add_daily_trend_gate,
    add_trendpullback_features,
    resample_ohlcv,
)

STRATEGY_NAME = "trendpullback_lumibot"
SYMBOL = "BTCUSDT"
TIMEFRAME = "H1"
DATA_SOURCE = "binance_spot"

# Module-level trade log — populated by the strategy during backtest,
# read by the main function after backtest completes.
_TRADE_LOG: list[dict] = []
_EQUITY_LOG: list[dict] = []


class TrendPullbackTrading(Strategy):
    """TrendPullback with actual order execution for Lumibot backtest engine.

    Signal detection logic is identical to poc/lumibot/strategy_lumibot.py.
    Adds: buy on entry signal, sell on EMA20 cross-down or max holding period.
    """

    parameters = {
        "pull": 0.3,
        "bn": 5,
        "en": 20,
        "max_hold_bars": 24,
        "run_id": "",
        "_h1": None,
        "_d1": None,
        "_m1": None,
    }

    def initialize(self):
        _TRADE_LOG.clear()
        _EQUITY_LOG.clear()
        self._h1 = self.parameters.get("_h1")
        self._d1 = self.parameters.get("_d1")
        self._m1 = self.parameters.get("_m1")
        self._run_id = self.parameters.get("run_id") or uuid.uuid4().hex[:12]
        self._pull = self.parameters.get("pull", 0.3)
        self._bn = self.parameters.get("bn", 5)
        self._en = self.parameters.get("en", 20)
        self._max_hold = self.parameters.get("max_hold_bars", 24)
        self._processed_bars: set[pd.Timestamp] = set()
        self._in_position = False
        self._entry_price = 0.0
        self._entry_ts: pd.Timestamp | None = None
        self._bars_held = 0
        self.sleeptime = "1H"

    def on_trading_iteration(self):
        if self._h1 is None or self._d1 is None:
            return

        dt = self.get_datetime()
        if dt is None:
            return

        current_ts = pd.Timestamp(dt)
        if current_ts.tz is None:
            current_ts = current_ts.tz_localize("UTC")
        else:
            current_ts = current_ts.tz_convert("UTC")

        h1_times = self._h1.index[self._h1.index <= current_ts]
        if len(h1_times) < 31:
            return

        t = h1_times[-1]
        if t in self._processed_bars:
            return
        self._processed_bars.add(t)

        cur = self._h1.loc[t]
        t_loc = self._h1.index.get_loc(t)

        # Record equity snapshot
        portfolio_val = self.get_portfolio_value()
        if portfolio_val is not None:
            _EQUITY_LOG.append({
                "ts": t,
                "equity": float(portfolio_val),
            })

        # --- Exit logic ---
        if self._in_position:
            self._bars_held += 1
            exit_signal = False

            # Exit condition 1: H1 close below EMA20
            if cur["close"] < cur["ema20"]:
                exit_signal = True

            # Exit condition 2: max holding period
            if self._bars_held >= self._max_hold:
                exit_signal = True

            if exit_signal:
                exit_price = float(cur["close"])
                self.sell_all()
                pnl = exit_price - self._entry_price
                _TRADE_LOG.append({
                    "entry_ts": self._entry_ts,
                    "exit_ts": t,
                    "entry_price": self._entry_price,
                    "exit_price": exit_price,
                    "side": "buy",
                    "pnl": pnl,
                    "bars_held": self._bars_held,
                })
                self._in_position = False
                self._entry_price = 0.0
                self._entry_ts = None
                self._bars_held = 0
            return

        # --- Entry signal detection (same as PoC) ---
        if t_loc < 1 or t_loc >= len(self._h1.index) - 1:
            return

        prev = self._h1.iloc[t_loc - 1]

        day = t.floor("D") - pd.Timedelta(days=1)
        if day not in self._d1.index:
            return
        d = self._d1.loc[day]
        trend = (d["close"] > d["ema20"]) and (d["ema20"] > d["ema20_prev"])
        if not trend:
            return

        near = abs(cur["low"] - cur["ema20"]) <= self._pull * cur["atr14"]
        if not near:
            return

        bullish = (cur["close"] > cur["open"]) and (cur["close"] > prev["high"])
        if not bullish:
            return

        vol_ok = (
            (cur["volume"] >= 0.9 * cur["vol_sma20"])
            if not np.isnan(cur["vol_sma20"])
            else False
        )
        if not (vol_ok and cur["atr14"] > 0):
            return

        # M1 micro breakout confirmation (if M1 data available)
        if self._m1 is not None:
            nt = self._h1.index[t_loc + 1]
            m1_idx = self._m1.index
            start_idx = m1_idx.searchsorted(t, side="right")
            end_idx = m1_idx.searchsorted(nt, side="right")
            ew = self._m1.iloc[start_idx:end_idx].copy()
            if len(ew) < max(self._en + 2, self._bn + 2):
                return
            ew["ema"] = ew["close"].ewm(span=self._en, adjust=False).mean()
            ew["hh"] = ew["high"].rolling(self._bn).max().shift(1)
            confirmed = False
            for _ts, r in ew.iterrows():
                if np.isnan(r["ema"]) or np.isnan(r["hh"]):
                    continue
                if r["close"] > r["hh"] and r["close"] > r["ema"]:
                    confirmed = True
                    break
            if not confirmed:
                return

        # Execute entry
        entry_price = float(cur["close"])
        btc_asset = Asset(SYMBOL[:3], asset_type="crypto")
        cash = self.get_cash()
        if cash is not None and cash > 0:
            qty = (cash * 0.95) / entry_price
            if qty > 0:
                order = self.create_order(btc_asset, quantity=qty, side="buy")
                self.submit_order(order)
                self._in_position = True
                self._entry_price = entry_price
                self._entry_ts = t
                self._bars_held = 0


def _build_backtest_output(
    run_id: str,
    start_ts: datetime,
    end_ts: datetime,
    sample: str | None = None,
) -> BacktestOutput:
    """Convert module-level trade/equity logs to a BacktestOutput."""
    now = datetime.now(tz=timezone.utc)

    run_metadata = RunMetadata(
        run_id=run_id,
        strategy=STRATEGY_NAME,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        start_ts=start_ts,
        end_ts=end_ts,
        run_ts=now,
        data_source=DATA_SOURCE,
        data_version="1",
        sample=sample,
    )

    trade_records: list[TradeRecord] = []
    for i, td in enumerate(_TRADE_LOG):
        entry_p = td["entry_price"]
        exit_p = td["exit_price"]
        pnl = td["pnl"]
        entry_ts = td["entry_ts"]
        exit_ts = td["exit_ts"]
        if isinstance(entry_ts, pd.Timestamp):
            entry_ts = entry_ts.to_pydatetime()
        if isinstance(exit_ts, pd.Timestamp):
            exit_ts = exit_ts.to_pydatetime()
        trade_records.append(TradeRecord(
            trade_id=f"{run_id}-t{i:04d}",
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            symbol=SYMBOL,
            side=td["side"],
            entry_price=entry_p,
            exit_price=exit_p,
            quantity=1.0,
            price_unit="USDT",
            quantity_unit=SYMBOL,
            gross_pnl=pnl,
            net_pnl=pnl,
            pnl_unit="USDT",
            holding_bars=td.get("bars_held"),
        ))

    # Build equity curve from logged snapshots
    equity_points: list[EquityCurvePoint] = []
    if _EQUITY_LOG:
        initial_equity = _EQUITY_LOG[0]["equity"]
        peak = initial_equity
        for j, eq in enumerate(_EQUITY_LOG):
            eq_val = eq["equity"]
            ts = eq["ts"]
            if isinstance(ts, pd.Timestamp):
                ts = ts.to_pydatetime()
            if eq_val > peak:
                peak = eq_val
            dd = (peak - eq_val) / peak if peak > 0 else 0.0
            prev_eq = _EQUITY_LOG[j - 1]["equity"] if j > 0 else eq_val
            ret_1d = (eq_val - prev_eq) / prev_eq if prev_eq > 0 else 0.0
            equity_points.append(EquityCurvePoint(
                ts=ts,
                equity=eq_val / initial_equity if initial_equity > 0 else 1.0,
                equity_unit="index",
                ret_1d=ret_1d,
                drawdown=dd,
            ))

    # Compute aggregate metrics from trades
    n_trades = len(trade_records)
    if n_trades > 0:
        returns = [t.net_pnl / t.entry_price for t in trade_records]
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r <= 0]

        equity_vals = [1.0]
        for r in returns:
            equity_vals.append(equity_vals[-1] * (1 + r))

        total_ret = equity_vals[-1] / equity_vals[0] - 1.0
        peak_eq = equity_vals[0]
        max_dd = 0.0
        for v in equity_vals:
            if v > peak_eq:
                peak_eq = v
            dd = (peak_eq - v) / peak_eq
            if dd > max_dd:
                max_dd = dd

        avg_ret = float(np.mean(returns))
        std_ret = float(np.std(returns, ddof=1)) if len(returns) > 1 else 1.0
        gross_profit = sum(r for r in returns if r > 0)
        gross_loss = abs(sum(r for r in returns if r <= 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else 0.0

        # Approximate annualization
        if trade_records[-1].exit_ts and trade_records[0].entry_ts:
            span = (trade_records[-1].exit_ts - trade_records[0].entry_ts).total_seconds()
            years = span / (365.25 * 86400) if span > 0 else 1.0
        else:
            years = 1.0
        ann_return = (1 + total_ret) ** (1 / years) - 1 if years > 0 else 0.0
        trades_per_year = n_trades / years if years > 0 else 0.0
        sharpe = (avg_ret / std_ret) * np.sqrt(trades_per_year) if std_ret > 0 else 0.0

        metrics = StrategyMetrics(
            total_return=total_ret,
            annual_return=float(ann_return),
            sharpe=float(sharpe),
            max_drawdown=max_dd,
            win_rate=len(wins) / n_trades,
            profit_factor=pf,
            avg_trade_return=avg_ret,
            avg_pnl_points=float(np.mean([t.net_pnl for t in trade_records])),
            trades=n_trades,
        )
    else:
        metrics = StrategyMetrics(total_return=0.0, trades=0)

    return BacktestOutput(
        run_metadata=run_metadata,
        equity_curve=equity_points,
        trades=trade_records,
        metrics=metrics,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backtest TrendPullback via Lumibot on real Binance BTC/USDT"
    )
    p.add_argument("--months", type=int, default=1, help="Lookback months for 1m data (default: 1)")
    p.add_argument("--out-dir", type=str, default=str(ROOT / "data" / "backtests"))
    p.add_argument("--no-influx", action="store_true", help="Skip InfluxDB write")
    p.add_argument("--dry-run", action="store_true", help="Print InfluxDB points without writing")
    p.add_argument("--sample", default="oos", help="Sample tag for InfluxDB (default: oos)")
    p.add_argument("--benchmark", default="BTC_BH", help="Benchmark tag (default: BTC_BH)")
    p.add_argument("--budget", type=float, default=100_000, help="Initial budget (default: 100000)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # 1) Fetch real 1m data
    print(f"[1/5] Fetching Binance {SYMBOL} 1m data ({args.months} months)...")
    m1_raw = fetch_ohlcv(symbol=SYMBOL, interval="1m", months=args.months)
    print(f"       bars={len(m1_raw)}, range={m1_raw['timestamp'].iloc[0]} ~ {m1_raw['timestamp'].iloc[-1]}")

    # Convert to indexed DataFrame for resampling
    m1 = m1_raw.set_index("timestamp")
    m1.index.name = "ts"

    # 2) Resample and compute features
    print("[2/5] Computing H1/D1 features...")
    h1 = add_trendpullback_features(resample_ohlcv(m1, "60min"))
    d1 = add_daily_trend_gate(resample_ohlcv(m1, "1D"))
    print(f"       H1 bars={len(h1)}, D1 bars={len(d1)}")

    # 3) Run Lumibot backtest
    print("[3/5] Running Lumibot backtest engine...")
    run_id = generate_run_id(STRATEGY_NAME, SYMBOL)

    btc_asset = Asset("BTC", asset_type="crypto")
    usd_asset = Asset("USD", asset_type="forex")

    # Trim dates to avoid DST edge cases (same as PoC)
    data_start = m1.index[0]
    data_end = m1.index[-1]
    bt_start = (data_start + timedelta(days=7)).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    bt_end = (data_end - timedelta(days=2)).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )

    lumi_df = m1.copy()
    if lumi_df.index.tz is not None:
        lumi_df.index = lumi_df.index.tz_localize(None)

    pandas_data = Data(
        btc_asset,
        lumi_df,
        date_start=bt_start,
        date_end=bt_end,
        timestep="minute",
        quote=usd_asset,
        timezone="UTC",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        TrendPullbackTrading.backtest(
            PandasDataBacktesting,
            backtesting_start=bt_start,
            backtesting_end=bt_end,
            pandas_data=[pandas_data],
            parameters={
                "pull": 0.3, "bn": 5, "en": 20, "max_hold_bars": 24,
                "run_id": run_id, "_h1": h1, "_d1": d1, "_m1": m1,
            },
            budget=args.budget,
            show_plot=False,
            show_tearsheet=False,
            save_tearsheet=False,
            save_stats_file=False,
            save_logfile=False,
            show_progress_bar=True,
            quiet_logs=True,
            stats_file=os.path.join(tmpdir, "stats.csv"),
            plot_file_html=os.path.join(tmpdir, "plot.html"),
            name="TrendPullback_Lumibot_BTC",
        )

    print(f"       trades={len(_TRADE_LOG)}, equity_snapshots={len(_EQUITY_LOG)}")

    # 4) Build BacktestOutput
    print("[4/5] Building BacktestOutput...")
    start_ts = data_start.to_pydatetime() if isinstance(data_start, pd.Timestamp) else data_start
    end_ts = data_end.to_pydatetime() if isinstance(data_end, pd.Timestamp) else data_end
    output = _build_backtest_output(
        run_id=run_id,
        start_ts=start_ts,
        end_ts=end_ts,
        sample=args.sample,
    )

    m = output.metrics
    print(f"       trades={m.trades}  sharpe={m.sharpe:.3f}  mdd={m.max_drawdown:.4f}  total_ret={m.total_return:.4f}")

    # Save JSON
    out_dir = Path(args.out_dir)
    paths = save_backtest_output(output, out_dir)
    print(f"       Saved: {paths['json']}")

    # 5) InfluxDB
    if args.no_influx:
        print("[5/5] InfluxDB write skipped (--no-influx)")
        return

    points = points_from_backtest(output, sample=args.sample, benchmark=args.benchmark)
    counts = Counter(p._name for p in points)

    if args.dry_run:
        print(f"[5/5] [DRY-RUN] points={len(points)}, counts={dict(counts)}")
        return

    url = os.getenv("INFLUX_URL", "http://localhost:8086")
    org = os.getenv("INFLUX_ORG", "quant_research")
    bucket = os.getenv("INFLUX_BUCKET", "nautilus_signals")
    token = (
        os.getenv("INFLUX_TOKEN")
        or os.getenv("DOCKER_INFLUXDB_INIT_ADMIN_TOKEN")
        or "change_me_super_secret_token"
    )

    from influxdb_client import InfluxDBClient
    from influxdb_client.client.write_api import SYNCHRONOUS

    with InfluxDBClient(url=url, token=token, org=org) as client:
        writer = client.write_api(write_options=SYNCHRONOUS)
        writer.write(bucket=bucket, org=org, record=points)

    print(f"[5/5] InfluxDB OK: points={len(points)}, counts={dict(counts)}, run_id={run_id}")


if __name__ == "__main__":
    main()
