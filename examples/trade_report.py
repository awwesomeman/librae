"""Build a tabular trade report from a BacktestOutput — a task librae
deliberately leaves to the caller.

Run with:
    python -m examples.trade_report

librae owns the backtest engine and typed results (BacktestOutput,
position_events) plus the correctness-sensitive position-lifecycle
reconstruction (compute_trade_lifecycle_outcomes/compute_trade_entry_outcomes)
and the cutoff-safe in-sample/out-of-sample split
(split_lifecycle_by_oos_start) — that logic has real edge cases (a lifecycle
opened in-sample and closed out-of-sample must not be misclassified) that are
worth getting right once instead of per-user. Report layout is ordinary user
code built on librae's public compute_* functions.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
from librae import (
    Backtest,
    Context,
    CostModel,
    OrderIntent,
    Strategy,
    compute_trade_entry_outcomes,
    compute_trade_lifecycle_outcomes,
    split_lifecycle_by_oos_start,
)


class AlternatingStrategy(Strategy):
    """Open then close every other bar — just enough trades to report."""

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        if ctx.positions.get(ctx.symbol):
            return [OrderIntent(action="close", symbol=ctx.symbol)]
        return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]


def _demo_ohlcv(periods: int = 40) -> pd.DataFrame:
    index = pd.date_range(datetime(2026, 1, 1, tzinfo=UTC), periods=periods, freq="1h")
    price = 100 + np.cumsum(np.random.default_rng(0).normal(0, 0.5, periods))
    return pd.DataFrame(
        {
            "open": price,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
            "volume": 1_000.0,
        },
        index=index,
    )


def main() -> None:
    symbol = "X"
    ohlcv = _demo_ohlcv()
    data = pd.concat({symbol: ohlcv}, names=["symbol", "datetime"])
    backtest = Backtest(
        data=data,
        strategy=AlternatingStrategy(),
        cost_model=CostModel.zero(),
        data_source="demo",
    )
    backtest.run()
    output = backtest.build_output()  # the only librae type this script needs

    ohlcv_by_symbol = {symbol: ohlcv}
    completed = compute_trade_lifecycle_outcomes(output.position_events, ohlcv_by_symbol)
    completed = completed[completed["status"] == "complete"]
    entry_outcomes = compute_trade_entry_outcomes(
        output.position_events, ohlcv_by_symbol, max_periods=5
    )

    completed.to_csv("trade_report.csv", index=False)

    # In-sample/out-of-sample split: split_lifecycle_by_oos_start is the part
    # librae owns (cutoff-safe against straddling lifecycles); everything
    # past this — which scopes and columns to publish — is your call.
    cutoff = ohlcv.index[len(ohlcv) // 2]
    for scope, (scoped_completed, _scoped_entries) in split_lifecycle_by_oos_start(
        completed, entry_outcomes, cutoff
    ).items():
        print(f"{scope}: {len(scoped_completed)} completed lifecycles")

    print(f"wrote trade_report.csv ({len(completed)} completed lifecycles)")


if __name__ == "__main__":
    main()
