# Performance analysis

`BacktestOutput` provides performance facts and a small set of metrics that
have stable definitions. It does not prescribe one report for every strategy
style. Benchmark construction, conditional comparisons, strategy aggregation,
and research attribution remain explicit caller choices.

## Add a benchmark

Pass a timezone-compatible price series before building the output:

```python
backtest.add_benchmark(benchmark_prices)
backtest.run()
output = backtest.build_output()

print(output.metrics.total_return)
print(output.metrics.benchmark_return)
print(output.metrics.tracking_error)
print(output.metrics.information_ratio)
```

The built-in comparison represents full-period buy-and-hold:

- the series must have a unique `DatetimeIndex` with the same timezone as the
  backtest timeline;
- a finite positive value must exist at or before the backtest start;
- observations are sorted and forward-filled onto the backtest timeline;
- the aligned series is normalized to the account's initial cash.

Choose the input that matches the intended comparison. For example, use an
adjusted-price or total-return index when distributions and corporate actions
should count. Librae does not fetch a benchmark, select one from the strategy
universe, or determine whether a forward-filled observation has become stale.

## Build a comparison frame

The equity curve contains both strategy and benchmark observations, so charts
and additional measures do not require a new engine abstraction:

```python
import pandas as pd

curve = pd.DataFrame(
    {
        "equity": [point.equity for point in output.equity_curve],
        "period_return": [
            point.period_return for point in output.equity_curve
        ],
        "benchmark_equity": [
            point.benchmark_equity for point in output.equity_curve
        ],
        "benchmark_period_return": [
            point.benchmark_period_return for point in output.equity_curve
        ],
    },
    index=pd.DatetimeIndex(point.ts for point in output.equity_curve),
)

curve["strategy_growth"] = curve["equity"] / curve["equity"].iloc[0]
curve["benchmark_growth"] = (
    curve["benchmark_equity"] / curve["benchmark_equity"].iloc[0]
)
curve["period_active_return"] = (
    curve["period_return"] - curve["benchmark_period_return"]
)
curve["total_return_gap"] = (
    curve["strategy_growth"] - curve["benchmark_growth"]
)
curve["relative_wealth"] = (
    curve["strategy_growth"] / curve["benchmark_growth"] - 1.0
)
```

`strategy_growth` and `benchmark_growth` are the two cumulative curves to
plot. Do not use the name `active_return` without specifying which definition
is intended:

| Measure | Definition | Typical use |
|---|---|---|
| Period active return | `r_strategy,t - r_benchmark,t` | Tracking error and information ratio |
| Total return gap | `R_strategy - R_benchmark` | Difference in cumulative-return percentage points |
| Relative wealth | `(1 + R_strategy) / (1 + R_benchmark) - 1` | Compounded relative performance |

Librae computes tracking error as the annualized sample standard deviation of
period active returns. Information ratio is their annualized mean divided by
that standard deviation. Both use
`RunConfig.reporting.periods_per_year`; information ratio is `None` when
tracking error is zero.

## Full period versus active periods

The built-in benchmark intentionally covers the full run. Comparing only
periods in which the strategy is active requires a policy that depends on the
strategy:

- include only intervals with an open position;
- scale benchmark returns by gross or net exposure;
- use a cash return while unexposed;
- compare each trade with the benchmark over its own holding interval.

These choices answer different questions, so the engine does not select one.
`EquityCurvePoint.exposed` describes the portfolio after that event; it is not
automatically an exposure mask for the return interval that ended at the same
event. Build interval ownership explicitly from the strategy's event,
position, or allocation facts before conditioning returns.

## Choose analysis by strategy style

There is no useful universal report beyond the common return, risk, cost, and
portfolio facts:

| Strategy style | Useful analysis |
|---|---|
| Single-asset directional | Full-period buy-and-hold, relative wealth, drawdown, trade outcomes, costs |
| Market-neutral or arbitrage | Absolute PnL, capital denominator, gross/net exposure, costs, and leg or spread attribution |
| Cross-sectional selection | Caller-defined universe benchmark, active return, tracking error, turnover, concentration, IC, and quantile returns |
| Multi-asset allocation | Caller-defined policy benchmark, allocation drift, exposure, turnover, and risk contribution |
| Multiple independent strategies | Aligned per-run returns and drawdowns before any caller-defined portfolio aggregation |

For arbitrage, a single asset's buy-and-hold return is usually not an
economically meaningful benchmark. For selection strategies, the caller must
define the eligible universe and weighting policy before an index comparison
or information coefficient has a stable meaning.

## Compare independent runs

One Librae run represents one account. Per-run returns can be aligned for
comparison without treating them as one portfolio:

```python
def period_returns(output):
    return pd.Series(
        [point.period_return for point in output.equity_curve],
        index=pd.DatetimeIndex(point.ts for point in output.equity_curve),
        name=output.run_metadata.run_id,
    )


comparison = pd.concat(
    [period_returns(output) for output in outputs],
    axis="columns",
    join="inner",
).sort_index()
```

Combining those runs into one portfolio additionally requires capital weights,
rebalance timing, currency conversion, calendar policy, and treatment of
overlapping exposure. That aggregation belongs in research or optional
orchestration, not in `BacktestOutput`.

## Detailed analysis and reporting

- Use `compute_trade_lifecycle_outcomes()` and
  `compute_trade_entry_outcomes()` for realized lifecycle and hypothetical
  post-entry analysis.
- Use the [signal outcome guide](signal-outcome-analysis.md) for gross forward
  return, MFE, and MAE before sizing and execution.
- Use [local artifact tables](local-artifacts.md) for pandas, notebook,
  Parquet, SQLite, or another caller-selected analysis workflow.
- Install `librae[analytics]` when using the optional QuantStats equity
  tearsheet or Matplotlib trade/signal reports.

Reporting renders facts produced by the engine. It must not silently change
the benchmark, annualization, active-period population, or portfolio
aggregation policy.
