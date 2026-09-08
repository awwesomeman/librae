# Replan delayed live targets from fresh completed-bar facts

Date: 2026-09-08
Status: Accepted

## Context

Live `PortfolioWeights` execution is deliberately serial because the supported
broker adapters do not provide a portable transactional basket order. A target
is replanned after every confirmed fill so broker price and quantity, rather
than a simulated fill, determine the remaining book.

The original lifecycle immediately queued the next leg after a resting order
filled. That replan reused the target's decision-time close, volume, lagged
ADV, and session usage. A bar or session transition could therefore size the
next leg from stale market facts. Live planning also consumed normalized
`can_buy` / `can_sell` fields only for grouped intents, leaving independent
intents and portfolio targets able to submit on an unavailable side.

## Decision

- Every live planning path uses the executor's shared side-tradability
  validator. An unavailable independent one-bar intent is skipped and audited;
  a grouped intent rejects its group before submission.
- A portfolio target preflights every required side before submitting any leg.
  Temporary side, coherent-snapshot, bar-volume, or ADV unavailability keeps
  the whole target pending only when `max_rebalance_delay_bars` supplies a
  positive completed-bar budget. Zero remains fail-closed.
- An immediately filled leg may be followed using the same completed-bar
  snapshot. Once a broker order rests, its later fill does not queue a
  successor. The runtime first observes a new coherent completed-bar event,
  resets session/ADV state, and then replans from confirmed cash and positions
  plus that event's close, volume, and lagged ADV.
- The current execution-bar timestamp, per-bar filled quantity, delay count,
  and latest attempted facts are checkpointed. Restart reconciliation may
  finish an already tracked order, but it cannot submit the next target leg
  until fresh completed bars arrive.
- Broker legs remain serial and non-atomic. `live_order_timeout_seconds`
  continues to bound time spent resting at the venue;
  `max_rebalance_delay_bars` bounds market-data events spent waiting between
  legs. `rebalance_residual_policy` remains a backtest-only slicing policy.

## Consequences

Live and simulated decisions now interpret `can_buy` / `can_sell` consistently.
A delayed live basket may take longer to complete, but it no longer presents an
old close or liquidity budget as current execution truth. Strategy evaluation
is serialized behind the pending target, so a new decision cannot race the
confirmed-state replan.

This supersedes only the sim/live exclusion described in
[`2026-09-04-bounded-whole-book-rebalance-deferral.md`](2026-09-04-bounded-whole-book-rebalance-deferral.md).
Backtest target-notional freezing, supersession, and residual-slicing semantics
are unchanged.

## References

- [`2026-09-04-bounded-whole-book-rebalance-deferral.md`](2026-09-04-bounded-whole-book-rebalance-deferral.md)
- [`2026-09-04-bounded-rebalance-residual-slicing.md`](2026-09-04-bounded-rebalance-residual-slicing.md)
- [`2026-09-07-grouped-close-legs-and-loud-preflight.md`](2026-09-07-grouped-close-legs-and-loud-preflight.md)
