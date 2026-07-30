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

## Choose the evaluation question first

A benchmark is not an intrinsic property of a strategy. First decide whether
the object being evaluated is a raw signal, an executed account, or only the
intervals in which the account took exposure:

| Evaluation object | Question | Suitable reference |
|---|---|---|
| Raw single-asset signal | Does the signal predict the next N observed bars? | Unconditional or matched same-horizon outcomes |
| Executed directional account | Was active management better than passively holding the asset for the whole run? | Full-period adjusted-price or total-return B&H |
| Active deployment | Did exposed intervals outperform their reference intervals? | Caller-defined active-period or exposure-scaled reference |
| Market-neutral/arbitrage account | Did the capital and risk budget earn an adequate net return? | Absolute return, cash hurdle, or strategy-specific spread; usually not one leg's B&H |

### Raw signal quality is not a B&H comparison

A B&H equity curve answers an investor-level opportunity-cost question. It
does not isolate whether individual signal events have predictive value. For a
raw signal, inspect direction-adjusted forward return, MFE, and MAE at fixed
horizons:

```python
from librae import compute_signal_outcomes


outcomes = compute_signal_outcomes(
    signal_timestamps,
    symbol_ohlcv,
    max_periods=20,
    direction="long",
)
horizon_20 = outcomes.loc[outcomes["bar_offset"] == 20]
```

Compare `horizon_20["forward_return"]` with a caller-defined unconditional or
matched sample using the same horizon, direction, calendar, and market regime.
The baseline policy remains research code because matching every bar, only
non-signal bars, or comparable volatility regimes answers different
questions. See the [signal outcome guide](signal-outcome-analysis.md).

### Full-period B&H can still be useful for an executed strategy

For an executed long-directional strategy, full-period B&H is a valid
secondary comparison: it tests whether timing, cash periods, costs, and risk
controls improved on passive ownership. It should not be presented as proof
that the underlying signal itself is predictive, and it is rarely meaningful
for short-biased, market-neutral, or arbitrage strategies.

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
strategy_column = outputs[0].run_metadata.run_id

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
    comparison[strategy_column] - comparison["benchmark"]
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
    comparison[[strategy_column, "benchmark"]],
    metrics=("wealth_index",),
)["wealth_index"]
relative_wealth = wealth[strategy_column] / wealth["benchmark"] - 1.0
```

Full-period, active-period, exposure-scaled, and per-trade comparisons answer
different questions. Build the intended population explicitly from order,
position, allocation, or exposure facts before calling the generic metrics.

For an active-period comparison, first construct an interval mask from the
strategy's position or allocation facts, then apply the same mask to both
return columns:

```python
# active_mask is a caller-built boolean Series indexed like comparison.
active_comparison = comparison.loc[
    active_mask,
    [strategy_column, "benchmark"],
]
active_returns = (
    active_comparison[strategy_column] - active_comparison["benchmark"]
).to_frame("active")
active_summary = summarize_performance(active_returns)
```

`EquityCurvePoint.exposed` describes portfolio state after that engine event.
Do not use it as the return interval's mask without explicitly assigning
interval ownership; otherwise entry and exit bars can be shifted by one
observation. Exposure-scaled comparisons additionally require a caller-chosen
gross, net, or directional exposure definition.

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
| Raw single-asset signal | matched same-horizon forward returns, hit rate, MFE, MAE |
| Executed single-asset directional | full-period B&H opportunity cost, active-period comparison, relative wealth, trade outcomes |
| Market-neutral or arbitrage | capital denominator, leg/spread attribution, gross/net exposure |
| Cross-sectional selection | eligible-universe benchmark, IC, quantile returns, sector attribution |
| Multi-asset allocation | policy benchmark, allocation drift, risk contribution |
| Multiple independent runs | capital weights, currency conversion, rebalance and overlap policy |

Trade metrics remain separate because period returns cannot reconstruct entry,
exit, holding-period, or notional facts. Use
`compute_trade_lifecycle_outcomes()` and `compute_trade_entry_outcomes()` for
trade analysis, and the [signal outcome guide](signal-outcome-analysis.md) for
gross forward return, MFE, and MAE.

## Use third-party reporting directly

Librae intentionally does not wrap an opinionated equity tearsheet. A thin
wrapper would either hide the reporting library's annualization defaults or
duplicate its evolving API. Install the library you choose and pass the
assumptions explicitly. For example, with QuantStats:

```bash
pip install quantstats
```

```python
import quantstats as qs


qs.reports.html(
    comparison[strategy_column],
    benchmark=comparison["benchmark"],
    rf=0.02,                 # annual risk-free rate chosen by the caller
    periods_per_year=252,    # justified for these aligned observations
    output="tearsheet.html",
    title="Strategy vs benchmark",
)
```

This example is appropriate only when `252` matches the observation and
calendar convention. For hourly, 24/7, irregular-event, or trade-return
series, resample or choose a justified convention before asking an external
library for annualized statistics.

Install `librae[analytics]` only for Librae's optional Matplotlib trade and
signal reports. Reporting may consume caller-prepared reference data, but it
must not silently redefine alignment, annualization, active periods, or
portfolio aggregation.
