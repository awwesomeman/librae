# One run owns one account and one execution venue

Date: 2026-09-09
Status: Accepted

## Context

Configuring two brokers for one run is refused in three places: route
resolution and `LiveTrader` in `librae/live/`, and `AccountConfig` in
`librae/core/run_config.py`. Each names the rule; none says why, or what
would have to change for cross-venue execution to be supported.

The reasoning existed but lived elsewhere — `architecture.md`'s statements
about what creates a strategy event and what is deliberately not built. So the
boundary read as a series of limitations. A caller could configure two brokers,
get an error naming a rule and not a reason, and have no way to tell whether
they were holding it wrong or asking for something out of scope.

## Decision

One run owns one account and executes through one venue. This is a scope
decision, not a missing feature.

**The decision cadence is a completed bar, not a quote.** A strategy event
exists only when a bar closes; a faster poll creates no additional decision
point. A cross-venue spread that survives a full bar is usually not an
arbitrage, so the opportunity this boundary excludes is largely one the engine
could not have acted on anyway.

**No venue offers cross-venue atomic execution.** A framework claiming it is
offering sequential submission plus failure reporting. Librae already provides
that shape through `group_id`, which preflights and checkpoints a complete
group before submitting serial broker requests — and which documents that it
does not claim broker or cross-venue atomicity. Adding a second venue would not
change what can be guaranteed; it would only make the guarantee harder to read.

**One account is one ledger.** Cash, PnL, and exposure are computed against a
single currency ledger. Spanning venues means FX conversion, transfers, and
cross-account netting, each an explicit non-goal with its own open question
(see issue #87 for the FX half).

What is supported today: a strategy coordinating several instruments on one
execution venue, including multi-leg groups; **market data from several
venues in one run**, routed per symbol, since reading a second venue commits
no capital and settles no ledger; and cross-venue strategies run as separate
runs whose sizing the caller owns. The boundary is execution, not data.

## Alternatives

**Allow multiple order adapters per run.** Rejected: it would present as
atomic what is sequential submission across venues, on a bar cadence that
cannot react to a leg failing.

**Keep the reasoning in `architecture.md` only.** Rejected: the reader who
needs it is holding an error message from one of the three sites, which is
what this ADR is linked from.

## Consequences

The three enforcement sites point here, so the error a caller hits leads to
the reasoning rather than restating it. Reopening this needs a concrete
strategy whose edge survives a completed-bar cadence, plus a defined answer for
the ledger question — not a request for two adapters.
