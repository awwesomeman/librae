# Signal outcome analysis

Librae can evaluate raw signal behavior without running a portfolio backtest
or connecting a database. This is useful for checking forward returns,
maximum favorable excursion (MFE), and maximum adverse excursion (MAE) before
execution and position sizing are introduced.

## Basic workflow

```python
from librae import compute_signal_outcomes, summarize_signal_mae_mfe

symbol_ohlcv = df.xs(symbol, level="symbol")
signal_ts = symbol_ohlcv.index[
    symbol_ohlcv["entry_signal"].astype(bool)
]

outcomes = compute_signal_outcomes(
    signal_ts,
    symbol_ohlcv,
    max_periods=60,
    direction="long",
    price_col="open",
)
summary = summarize_signal_mae_mfe(
    signal_ts,
    symbol_ohlcv,
)
```

Charting `summary` (e.g. median/p75 MFE-MAE by horizon) is caller-owned — see
`examples/trade_report.py` for the compute → chart pattern used elsewhere in
librae.

## Interpretation

- Analyze one symbol at a time. Cross-sectional aggregation belongs in the
  research layer, where weighting and sampling assumptions can be explicit.
- A signal observed on bar T uses the next observed bar's selected price field
  as its reference. Offset 1 begins on the observed bar after that reference.
- `direction` is explicit and independent of whether the source event is
  labeled entry or exit.
- Returns, MFE, and MAE are gross hypothetical percentage outcomes. They do
  not include commissions, slippage, liquidity, position sizing, or other
  execution constraints.
- MFE and MAE are non-negative excursion magnitudes.
- Recent signals have shorter forward histories. Compare the valid sample count
  at each horizon instead of assuming a constant denominator.

Use these functions to study the signal itself. Use `Backtest` when you need
portfolio state, fills, costs, risk controls, or performance metrics. The
database schema and optional Grafana signal monitor are described in
[Optional infrastructure](optional-infrastructure.md).
