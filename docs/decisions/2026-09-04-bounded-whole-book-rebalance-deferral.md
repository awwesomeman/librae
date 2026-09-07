# Bound whole-book rebalance deferral by explicit tradability

Date: 2026-09-04
Status: Accepted

## Context

Cross-market portfolios need each asset's last close to value the whole account
on a shared clock, but a local holiday must not create a market observation.
Treating forward-filled OHLC fields as fresh can materially change a rotation
strategy's measured return. Librae already retains last observed closes for
valuation, so historical panels should remain sparse on closed-market events.

Librae already accepts paired `can_buy` and `can_sell` facts. They separate
side-level execution availability from OHLCV without teaching the engine every
venue calendar, halt, auction, or price-limit rule. Before this decision,
`PortfolioWeights` failed atomically when one required side was unavailable;
it could not wait for a later common execution event.

## Decision

`ExecutionPolicy.max_rebalance_delay_bars` provides a backtest-only, bounded
whole-book wait:

- zero keeps fail-fast next-bar execution;
- a positive value permits that many unavailable global data events after the
  normal T+1 eligibility point;
- every actual reduction, close, addition, and reversal side is preflighted
  before any position mutation;
- the target is recalculated from current positions, equity, and execution
  prices on every retry;
- a newer complete target supersedes an older unfilled target, is recorded as
  `decision_skipped/rebalance_superseded`, and does not reset the delay budget;
- exceeding the bound or reaching sample end raises instead of partially
  filling or silently discarding the rebalance.

The strategy still receives every data event. Complete portfolio targets use
latest-target-wins semantics because an unfilled older allocation is no longer
the strategy's desired state. Symbol-level intents cannot be mixed into that
pending target and fail loudly instead.

This does not reverse the data-readiness decision in
[`2026-08-05-grouped-decisions-no-engine-side-waiting.md`](2026-08-05-grouped-decisions-no-engine-side-waiting.md).
Strategies must still return grouped decisions only when every required symbol
has a current event bar. The bounded wait begins later, at execution time, and
only for explicit side untradability or an unavailable configured fill field.

## Consequences

Data adapters and ETL remain responsible for leaving closed-market observations
absent. `can_buy` / `can_sell` are execution-side facts on real bars, not a
valuation-only marker: conflating them would hide a carried row from fills but
still expose it to signals, holding age, and financing. Independent
`OrderIntent`s retain their existing per-symbol timing, and sim/live behavior
is unchanged. Supporting durable cross-cycle rebalance deferral in sim/live
would require separate checkpoint and broker-order lifecycle semantics and is
intentionally not implied by this backtest policy.
