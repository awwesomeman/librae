# 2026-08-24 — Financing cost has no off switch

> Status: implemented

## Background

Since `a762efb` the engine charges a short position borrow interest from a
`borrow_rate` bar column, in sim and live as well as backtest. That changed
behaviour for a strategy whose author had deliberately ignored borrow cost:
`funding_rate_arb_btcusdt` documents that BTC cross-margin runs about 0.4% APR
and is negligible against the funding it harvests. The engine started charging
it without telling anyone.

The measured rate was 0.441% and the backtest still returned +10.8%, so that
author's assumption held. But the request it prompted — "let the user choose
whether borrow cost is counted" — is one that will be raised again, and the
obvious implementation is wrong.

## Decision

There is no flag to disable financing cost. A caller who needs a different
answer supplies a different **rate source**, and a rate of zero is a valid
answer.

`_bind_market_data_source` finds the rate source by duck typing
(`getattr(source, "fetch_borrow_rate_history", None)`), so a caller overrides
only that one method and keeps the adapter's market data:

```python
class ZeroBorrow:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)  # OHLCV etc. unchanged

    def fetch_borrow_rate_history(self, symbol, limit=None, *, since=None):
        return pd.DataFrame(
            {"ts": [...], "borrow_rate": [0.0], "rate_period_seconds": [86400.0]}
        )
```

Pass it through `data_adapter_overrides`. The same seam carries an equity
broker's stock-loan rates, which no crypto adapter could answer.

## Alternatives considered (not adopted)

- **`charge_borrow: bool`.** Selling short borrows the asset, so the cost is a
  fact rather than a preference, and the flag would let a caller declare a
  known cost absent. It also has no place to live: whether a *decision*
  weighs borrow cost is the strategy's business, and strategies already read
  the rate and decide for themselves — the engine's job is only to record what
  was paid.

  The decisive objection is observability. A zero rate and a disabled charge
  are not equivalent:

  | | `financing_cash_flows` rows |
  |---|---|
  | `borrow_rate = 0.0` | one row, `kind='borrow'`, `cash_flow=0` |
  | `borrow_rate` absent | none |

  A zero rate is a claim on the record that this position cost nothing. A
  disabled charge is a blank, indistinguishable from the three ways a rate can
  already go missing — the asset cannot be borrowed, the data was not
  published, or the fetch dropped it. A flag would add a fourth. Those three
  are already hard enough to tell apart that
  [`librae/core/financing.py`] refuses to read a missing rate as a free one.

- **A named `borrow_rate_sources=` parameter.** Sugar over the duck-typed seam
  that already works, so it buys nothing until a real caller finds composition
  insufficient.

- **A per-broker rate dimension.** Proposed for equities, where stock-loan
  rates differ by broker as well as by symbol. Unnecessary: one run binds one
  source per adapter, which *is* the broker view, and per-symbol differences
  are that source's business. Two brokers quoting the same symbol is two
  accounts and two runs.

## Consequences

A strategy that ignores borrow cost in its entry logic keeps doing so; only
its accounting changed, and it should verify its assumption rather than
suppress the charge — `funding_rate_arb_btcusdt` did, and the assumption
survived.

A caller with no rate source at all still gets no charge, because there is no
rate to charge. That outcome is announced (see `f4f8a3f`) rather than
inferred, which is the difference this decision is protecting.

## References

- [`librae/core/financing.py`] — the accrual rules and why a missing rate is
  not a free one.
- [`2026-08-05-grouped-decisions-no-engine-side-waiting.md`](2026-08-05-grouped-decisions-no-engine-side-waiting.md)
  — the engine failing loudly rather than silently absorbing a caller's gap.

[`librae/core/financing.py`]: ../../librae/core/financing.py
