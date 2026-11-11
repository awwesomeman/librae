"""Synthetic query results for populated console tests; no database connection."""

from __future__ import annotations

import numpy as np
import pandas as pd
from librae.console import queries


def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    current = pd.Timestamp.now(tz="UTC").floor("min")
    times = pd.date_range(end=current.floor("h") - pd.Timedelta(hours=1), periods=240, freq="h")
    x = np.arange(len(times))
    close = 60000 + x * 12 + np.sin(x / 8) * 500
    bars = pd.DataFrame(
        {
            "ts": times,
            "open": close - 60,
            "high": close + 150,
            "low": close - 150,
            "close": close,
            "volume": 100 + x,
        }
    )
    runs = pd.DataFrame(
        [
            {
                "run_id": key,
                "strategy_name": name,
                "mode": mode,
                "timeframe": "H1",
                "run_at": current.floor("D") - pd.Timedelta(days=days),
                "poll_seconds": poll,
                "last_heartbeat_at": current - pd.Timedelta(seconds=age) if poll else pd.NaT,
            }
            for key, name, mode, days, poll, age in [
                ("run-trend", "Momentum Trend", "sim", 1, 30, 10),
                ("run-signals", "Signal Lab", "backtest", 2, None, 0),
                ("run-carry", "Funding Carry", "live", 3, 30, 10),
                ("run-breakout", "Volatility Breakout", "live", 5, 60, 480),
                ("run-backtest", "Mean Reversion", "backtest", 10, None, 0),
                ("run-trend-backtest", "Momentum Trend", "backtest", 12, None, 0),
            ]
        ]
    )
    equity = pd.DataFrame(
        {
            "ts": times,
            "equity": 100000 + x * 55 + np.sin(x / 10) * 800,
            "gross_exposure": 0.6 + np.sin(x / 12) * 0.1,
            "net_exposure": 0.2 + np.sin(x / 14) * 0.1,
            "concentration": 0.35,
        }
    )
    events = pd.DataFrame(
        [
            {
                "ts": times[i],
                "symbol": "BTCUSDT",
                "event_type": "close",
                "side": "long",
                "fill_quantity": 0.2,
                "price": close[i],
                "notional": close[i] * 0.2,
                "cash_flow": close[i] * 0.2,
                "realized_pnl": (close[i] - close[i - 10]) * 0.2,
                "net_return": (close[i] / close[i - 10] - 1) * 100,
                "margin_roi": 2.0,
                "margin_mode": "dynamic",
                "entry_price": close[i - 10],
                "entry_at": times[i - 10],
                "remaining_quantity": 0.0,
                "commission": 2.0,
                "slippage": 1.0,
                "tax": 0.0,
                "group_id": None,
                "periods_held": 10,
                "reason": "Signal exit",
            }
            for i in range(20, 240, 15)
        ]
    )
    if sql == queries.RUNS:
        return runs
    if sql == queries.SIGNAL_RUNS:
        return runs.loc[runs["run_id"] != "run-breakout"].reset_index(drop=True)
    if sql == queries.RUN_STATE:
        row = runs.loc[runs.run_id == params[0]].iloc[0].to_dict()
        row.update(halted=params[0] == "run-carry", last_decision_bar=times[-1])
        return pd.DataFrame([row])
    if sql == queries.RUN_ACCOUNTS:
        if params[0] == "run-signals":
            return pd.DataFrame(columns=["account_id", "currency"])
        return pd.DataFrame(
            {"account_id": ["USD-main", "USD-secondary"], "currency": ["USD", "USD"]}
        )
    if sql == queries.ACCOUNT_OVERVIEW:
        overview = runs.assign(
            account_id="USD-main",
            currency="USD",
            last_equity_at=times[-1],
            equity=runs["run_id"].map(
                {
                    "run-trend": 113200.0,
                    "run-carry": 105400.0,
                    "run-breakout": 98750.0,
                    "run-backtest": 124800.0,
                    "run-trend-backtest": 120000.0,
                }
            ),
            gross_exposure=0.65,
            net_exposure=0.2,
            concentration=0.35,
            open_positions=runs["run_id"].map(
                {
                    "run-trend": 2,
                    "run-carry": 2,
                    "run-breakout": 1,
                    "run-backtest": 0,
                    "run-trend-backtest": 0,
                }
            ),
            halted=runs["run_id"] == "run-carry",
        )
        overview.loc[overview["run_id"] == "run-signals", ["account_id", "currency"]] = None
        overview.loc[overview["run_id"] == "run-signals", "last_equity_at"] = pd.NaT
        overview.loc[
            overview["run_id"] == "run-signals",
            ["equity", "gross_exposure", "net_exposure", "concentration", "open_positions"],
        ] = np.nan
        return overview
    if sql == queries.LATEST_EQUITY:
        value = 113200.0 if params[1] == "USD-main" else 25000.0
        return pd.DataFrame({"ts": [times[-1]], "equity": [value]})
    if sql == queries.EQUITY:
        return equity.iloc[::-1].reset_index(drop=True)
    if sql == queries.PERFORMANCE:
        return pd.DataFrame(
            [
                {
                    "currency": "USD",
                    "net_pnl": 11250.0,
                    "total_return": 0.1125,
                    "max_drawdown": -0.034,
                    "period_sharpe": 1.84,
                    "period_sortino": 2.31,
                    "win_rate": 0.586,
                    "profit_factor": 1.72,
                    "trades": 128,
                }
            ]
        )
    if sql == queries.OPEN_POSITIONS:
        return pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "side": side,
                    "remaining_quantity": quantity,
                    "entry_price": price,
                    "entry_at": times[-12],
                    "price": price,
                    "fill_quantity": quantity,
                    "notional": price * quantity,
                    "margin_locked": price * quantity / 2,
                    "leverage": 2.0,
                    "liquidation_price": price * (0.55 if side == "long" else 1.45),
                    "margin_mode": "dynamic" if symbol == "BTCUSDT" else "fixed",
                }
                for symbol, side, quantity, price in [
                    ("BTCUSDT", "long", 0.4, 62000.0),
                    ("ETHUSDT", "short", 4.0, 3200.0),
                ]
            ]
        )
    if sql == queries.LATEST_MARKS:
        return pd.DataFrame(
            {"symbol": ["BTCUSDT", "ETHUSDT"], "ts": [times[-1]] * 2, "close": [close[-1], 3150.0]}
        )
    if sql == queries.POSITION_EVENTS:
        return events.iloc[::-1].reset_index(drop=True)
    if sql in (queries.BAR_SYMBOLS, queries.SIGNAL_SYMBOLS):
        return pd.DataFrame({"symbol": ["BTCUSDT"]})
    if sql == queries.RUN_BARS:
        return bars.iloc[::-1].reset_index(drop=True)
    if sql == queries.SIGNALS:
        return (
            pd.DataFrame({"ts": times[10::3], "signal_value": 1.0})
            .iloc[::-1]
            .reset_index(drop=True)
        )
    if sql == queries.SIGNAL_SUMMARY:
        return pd.DataFrame(
            {
                "signal_count": [len(times[10::3])],
                "last_ts": [times[10::3][-1]],
                "last_value": [1.0],
            }
        )
    if sql == queries.SIGNAL_REFERENCE:
        return bars.loc[bars["ts"] > params[2]].head(1)
    if sql == queries.RUNTIME_EVENTS:
        return pd.DataFrame(
            [
                {
                    "ts": current - pd.Timedelta(minutes=i * 9),
                    "event_type": kind,
                    "symbol": symbol,
                    "reason": reason,
                    "detail": '{"source": "synthetic preview"}',
                }
                for i, (kind, symbol, reason) in enumerate(
                    [
                        ("runtime_started", "", "Runtime checkpoint restored"),
                        ("decision_skipped", "BTCUSDT", "No newly completed bar"),
                        ("halted", "ETHUSDT", "Reconciliation requires operator review"),
                    ]
                )
            ]
        )
    raise AssertionError("Unexpected console query")
