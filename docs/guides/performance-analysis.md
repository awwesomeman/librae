# Performance analysis

Librae exposes engine facts plus two generic period-return APIs. It does not
assign semantic roles such as strategy or benchmark, align unrelated data, or
choose annualization and grouping rules:

- `summarize_performance()` returns one full-sample DataFrame with metrics on
  the index and input series on the columns.
- `compute_performance_series()` returns `{metric: DataFrame}` for path
  metrics with the input index and columns unchanged.

Both functions accept an already-aligned, finite `pd.DataFrame` whose unique,
sorted `DatetimeIndex` is timezone-aware. Every column is simply a named
return series, so the same API supports one strategy, several strategies, or
caller-selected reference series.

## Analyze one run

```python
import pandas as pd

from librae import compute_performance_series, summarize_performance


period_returns = pd.DataFrame(
    {
        output.run_metadata.run_id: [
            point.period_return for point in output.equity_curve
        ]
    },
    index=pd.DatetimeIndex(point.ts for point in output.equity_curve),
)

summary = summarize_performance(
    period_returns,
    metrics=(
        "total_return",
        "mean_period_return",
        "period_volatility",
        "period_sharpe",
        "max_drawdown",
    ),
)
paths = compute_performance_series(
    period_returns,
    metrics=("cumulative_return", "drawdown"),
)
```

`period_sharpe`, `period_sortino`, volatility, and downside deviation preserve
the input observation frequency. Pass `period_target_return` at analysis time
when the target is already expressed at that same frequency:

```python
summary = summarize_performance(
    period_returns,
    metrics=("period_sharpe", "period_sortino"),
    period_target_return=0.0001,
)
```

This keeps a run reproducible without pretending that hourly, irregular-event,
and daily strategies share one annualization policy.

## Compare strategies and reference series

Build and align each return series explicitly, then pass them as columns:

```python
def returns_from_output(output):
    return pd.Series(
        [point.period_return for point in output.equity_curve],
        index=pd.DatetimeIndex(point.ts for point in output.equity_curve),
        name=output.run_metadata.run_id,
    )


strategy_returns = pd.concat(
    [returns_from_output(output) for output in outputs],
    axis="columns",
    join="inner",
).sort_index()

benchmark_returns = benchmark_prices.pct_change().rename("benchmark")
comparison = pd.concat(
    [strategy_returns, benchmark_returns],
    axis="columns",
    join="inner",
).dropna()

summary = summarize_performance(comparison)
paths = compute_performance_series(
    comparison,
    metrics=("wealth_index", "drawdown"),
)
```

The caller owns the join, calendar, resampling, price adjustment, missing-data,
and staleness policies. Librae therefore has no `add_benchmark()` or
`add_strategies()` API: those names would add roles without removing any of the
decisions that make the comparison valid.

## Active-return analysis

Period active return is ordinary column arithmetic:

```python
active_returns = (
    comparison["strategy"] - comparison["benchmark"]
).to_frame("active")

active_summary = summarize_performance(
    active_returns,
    metrics=("mean_period_return", "period_volatility", "period_sharpe"),
)
```

For this input, `period_volatility` is nonannualized tracking error and
`period_sharpe` is a nonannualized information ratio. Keeping the generic names
in the API avoids silently assuming which column is a benchmark.

Do not compound arithmetic active returns to obtain relative wealth. Use the
two wealth paths instead:

```python
wealth = compute_performance_series(
    comparison[["strategy", "benchmark"]],
    metrics=("wealth_index",),
)["wealth_index"]
relative_wealth = wealth["strategy"] / wealth["benchmark"] - 1.0
```

Full-period, active-period, exposure-scaled, and per-trade comparisons answer
different questions. Build the intended population explicitly from order,
position, allocation, or exposure facts before calling the generic metrics.

## Grouping and annualization

By-year or regime analysis is caller-side grouping:

```python
by_year = {
    year: summarize_performance(group)
    for year, group in period_returns.groupby(period_returns.index.year)
}
```

Annualize only after choosing a justified `periods_per_year` for the analyzed
series. For example:

```python
periods_per_year = 252
annualized_volatility = (
    summary.loc["period_volatility"] * periods_per_year**0.5
)
annualized_sharpe = summary.loc["period_sharpe"] * periods_per_year**0.5
```

For irregular observations or trade returns, a fixed square-root scaling may
not be meaningful. Keep the raw period or trade metric, or implement the
strategy-specific time convention in reporting code.

## Strategy-specific analysis

The generic APIs deliberately stop before economic interpretation:

| Strategy style | Additional caller-owned analysis |
|---|---|
| Single-asset directional | buy-and-hold reference, relative wealth, trade outcomes |
| Market-neutral or arbitrage | capital denominator, leg/spread attribution, gross/net exposure |
| Cross-sectional selection | eligible-universe benchmark, IC, quantile returns, sector attribution |
| Multi-asset allocation | policy benchmark, allocation drift, risk contribution |
| Multiple independent runs | capital weights, currency conversion, rebalance and overlap policy |

Trade metrics remain separate because period returns cannot reconstruct entry,
exit, holding-period, or notional facts. Use
`compute_trade_lifecycle_outcomes()` and `compute_trade_entry_outcomes()` for
trade analysis, and the [signal outcome guide](signal-outcome-analysis.md) for
gross forward return, MFE, and MAE.

Install `librae[analytics]` only when using optional QuantStats or Matplotlib
reports. Rendering may consume caller-prepared reference data, but it must not
silently redefine alignment, annualization, active periods, or portfolio
aggregation.
