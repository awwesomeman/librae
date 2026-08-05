# Grouped decisions no longer wait across periods

Date: 2026-08-05
Status: Accepted

## Context

`PortfolioTargets`/`MultiLegOrder` decisions used to wait for every required
symbol to have a bar before executing (`partition_pending_decision`), and
that wait was implemented by gating the entire `on_bar` callback: both
engines skipped calling `strategy.on_bar` at all while the pending decision
was still a grouped type. One symbol's temporary data gap therefore blocked
decisions for every other, unrelated symbol the same strategy instance
managed — observed running a multi-position portfolio strategy where a
single symbol's data gap silently froze every other position's decisions for
as long as the gap lasted.

Researched how mainstream event-driven engines (Backtrader, Zipline,
NautilusTrader) handle this. The framework's job is strictly *correct
time-ordered delivery of data*: a NautilusTrader multi-dataset time-sync bug
is tracked as a framework issue (nautechsystems/nautilus_trader#1515).
Whether a strategy's own multi-symbol decision is ready to fire is left
entirely to the strategy, checked against whatever data has already arrived
— there is no framework-side "remember an incomplete decision and auto-fire
it once data catches up" concept in any of them.

Librae already gives strategies exactly the primitive needed for this:
`Context.available_symbols` (`tuple(self.bars)`, i.e. symbols with a bar this
period). `examples/multi_leg_spread/strategy.py` already self-checked with it
before deciding whether to emit a `MultiLegOrder` at all — the engine's
cross-period queueing was redundant with what a correctly-written strategy
already does, and directly contradicted `MultiLegOrder`'s own docstring
promise of "one synchronous market-data event."

## Decision

Grouped decisions must be immediately actionable when returned:

- `validate_strategy_decision` rejects a `PortfolioTargets`/`MultiLegOrder`
  outright (`ValueError`) if any required symbol (every non-zero target/leg,
  plus every currently held position for `PortfolioTargets`) is missing from
  `bars` at the moment it's returned. The strategy must check
  `ctx.available_symbols` itself before returning one.
- Since a grouped decision can never sit "pending" across periods anymore,
  `partition_pending_decision` returns it as fully ready unconditionally, and
  the `on_bar`-blocking gate in both engines is removed entirely —
  `strategy.on_bar` is called every period, unconditionally.
- Independent per-symbol `OrderIntent` queueing (waiting for *its own*
  symbol's next bar) is unchanged: it's narrow, never blocks unrelated
  symbols, and was never the problem.
- `execute_pending_decision_and_stops` gained an explicit guard before
  unwrapping `MultiLegOrder.legs`: `execute_order_intents` silently skips a
  leg with no price instead of raising, which would otherwise let a leg that
  lost its bar between decision time and backtest's one-bar execution defer
  fill alone and leave a naked position. This case now raises instead of
  filling asymmetrically.
- `LiveRuntimeState.pending_decision` narrows from `StrategyDecision` to
  `list[OrderIntent]`, since it can no longer hold a grouped value. Checkpoint
  schema bumped `v16 -> v17`; old checkpoints are rejected outright, per this
  repo's established no-migration policy.

This removes code rather than adding new engine state: no new pending-slot
field, no new `Context` field, and `merge_pending_decisions` reverts to its
simpler pre-`MultiLegOrder` scope (pure per-symbol list merging).
`execute_portfolio_targets`'s rebalance math, `MultiLegOrder`'s synchronous
backtest execution, and `LiveRebalance`'s replan-from-actual-fill sequencing
are unrelated to this change and are untouched.

## Consequences

Strategy authors returning `PortfolioTargets`/`MultiLegOrder` must guard with
an explicit `ctx.available_symbols` (or equivalent) check before returning
one, or the engine now fails loudly at decision time instead of silently
queueing. Two of the four bundled examples already did this
(`multi_leg_spread`, `minimum_variance`); `target_weights` and
`topk_selection` did not and were updated.

This is an intentional breaking change with no compatibility shim, consistent
with [`2026-07-28-strategy-decision-execution-naming.md`](2026-07-28-strategy-decision-execution-naming.md).

## References

- [NautilusTrader #1515: streaming backtest multi-dataset time sync](https://github.com/nautechsystems/nautilus_trader/issues/1515)
- [`2026-07-28-strategy-decision-execution-naming.md`](2026-07-28-strategy-decision-execution-naming.md)
