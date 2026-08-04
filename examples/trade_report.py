"""Build a trade report (charts) from a BacktestOutput — a task librae
deliberately leaves to the caller.

Run with:
    python -m examples.trade_report

librae owns the backtest engine and typed results (BacktestOutput,
order_events) plus the correctness-sensitive position-lifecycle
reconstruction (compute_trade_lifecycle_outcomes/compute_trade_entry_outcomes)
and the cutoff-safe in-sample/out-of-sample split
(split_lifecycle_by_oos_start) — that logic has real edge cases (a lifecycle
opened in-sample and closed out-of-sample must not be misclassified) that are
worth getting right once instead of per-user. How the results get charted or
reported is not: librae only ships one opinionated chart, the K-line/marker
overlay (`librae.plot_kbars`), because there's essentially one correct way to
draw it. Everything below is ordinary user code built on librae's public
compute_* functions.
"""

from __future__ import annotations

from datetime import UTC, datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
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
    """Open then close every other bar — just enough trades to chart."""

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
    completed = compute_trade_lifecycle_outcomes(output.order_events, ohlcv_by_symbol)
    completed = completed[completed["status"] == "complete"]
    entry_outcomes = compute_trade_entry_outcomes(
        output.order_events, ohlcv_by_symbol, max_periods=5
    )

    # --- your chart, your code: librae only supplies the DataFrames above ---
    durations = completed["periods_held"].dropna().astype(int).tolist()
    pnl_curve = completed["net_pnl"].cumsum().tolist()

    fig, (duration_ax, pnl_ax) = plt.subplots(1, 2, figsize=(10, 3.5))
    duration_ax.hist(durations, bins=min(10, max(1, len(set(durations)))))
    duration_ax.set_title("Position duration (bars)")
    pnl_ax.plot(range(1, len(pnl_curve) + 1), pnl_curve)
    pnl_ax.axhline(0, color="grey", linewidth=0.7)
    pnl_ax.set_title("Cumulative PnL by lifecycle")
    fig.tight_layout()
    fig.savefig("trade_report.png", dpi=130)
    plt.close(fig)

    # In-sample/out-of-sample split: split_lifecycle_by_oos_start is the part
    # librae owns (cutoff-safe against straddling lifecycles); everything
    # past this — how many scopes, how they're laid out — is your call.
    cutoff = ohlcv.index[len(ohlcv) // 2]
    for scope, (scoped_completed, _scoped_entries) in split_lifecycle_by_oos_start(
        completed, entry_outcomes, cutoff
    ).items():
        print(f"{scope}: {len(scoped_completed)} completed lifecycles")

    print(f"wrote trade_report.png ({len(durations)} completed lifecycles)")


if __name__ == "__main__":
    main()
