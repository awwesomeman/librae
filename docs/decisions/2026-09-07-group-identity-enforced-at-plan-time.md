# Group identity is enforced when an intent is planned, never on a confirmed fill

Date: 2026-09-07
Status: Accepted

## Context

Issue #113 established that one net position carries one `group_id` through
its add, close, trade, and financing records, and asked that a scale-in of a
different identity — grouped-to-ungrouped and ungrouped-to-grouped included
— be rejected "for simulated and externally confirmed scale-ins", failing
"before any grouped batch mutation".

Two implementations of that request each broke something the request did
not anticipate:

- Enforcing it on an *externally confirmed* fill (issue #106's first cut)
  refused a fill the venue had already executed. The order stayed tracked as
  submitted with zero filled quantity while the venue held the fill, nothing
  halted, and every later poll raised again — the one failure shape that
  lets the local book drift from the venue silently.
- Enforcing it *inside* the simulated execution loop (issue #113's first
  cut) raised after earlier ungrouped intents in the same decision had
  already committed, and fired on the ungrouped additions a
  `PortfolioWeights` target generates, so any rebalance that increased a
  position a group had opened aborted the run — in live, halted the account.

Mainstream practice separates the two moments cleanly. Venue and prime-broker
systems treat a fill as settled fact; attribution to a book, strategy, or
sub-account is a ledger concern, and a mismatch surfaces as a reconciliation
break, never as a refused execution. Whether an order *should be generated*
that mixes books is a decision for the order planner, made before anything
is sent.

## Decision

- The identity check runs once, at the top of simulated intent execution,
  over every intent in the batch and against the book as it stands before
  the first intent executes. A violation anywhere in the batch raises
  `ValueError` with nothing mutated. This satisfies "before any grouped
  batch mutation" literally and closes the window in which an earlier
  ungrouped intent had already moved the book.
- `PortfolioWeights` refuses, at planning time and before its own reductions
  execute, any target that would add to a position a group opened. The
  message names the symbols and the two remedies: close the group, or manage
  the symbol with grouped intents. Reductions and flips are not scale-ins —
  they carry the position's identity out — and stay allowed.
- A confirmed fill is always booked. Applying it sets the group on a newly
  opened position and leaves an existing position's group unchanged; the
  add event records the fill's own group so the ledger shows what was
  actually sent. This is the one item of #113's definition of done that is
  deliberately not implemented as written, for the reason above.

## Consequences

A symbol is managed by one attribution model at a time. A strategy that
pair-trades a symbol under a `group_id` cannot also raise its weight through
`PortfolioWeights` without closing the group first; the first such target
fails on the bar it is returned, in backtest before live. Live order
planning replays the same simulated execution, so it rejects the same shape
through the same code path, and a strategy that backtests clean does not
meet a new halt in production.

The unit tests that asserted the in-loop raise were replaced by ones that
assert the earlier ungrouped intent is *not* filled, which the original
tests did not exercise.

## References

- [`2026-09-07-grouped-close-legs-and-loud-preflight.md`](2026-09-07-grouped-close-legs-and-loud-preflight.md)
- [`2026-08-05-grouped-decisions-no-engine-side-waiting.md`](2026-08-05-grouped-decisions-no-engine-side-waiting.md)
- Issues #106 and #113
