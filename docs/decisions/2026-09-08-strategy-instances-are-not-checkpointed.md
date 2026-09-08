# Keep strategy instances outside the runtime checkpoint

Date: 2026-09-08
Status: Accepted

## Context

The sim/live checkpoint persists engine-owned execution state, including cash,
positions, orders, pending decisions, counters, and market-data watermarks. A
strategy object can also hold arbitrary mutable Python state such as cooldowns,
online estimators, or hedge flags, but that state has no schema, version, or
atomic relationship with an engine checkpoint.

When `Strategy.on_bar` raises, the data event remains uncommitted and is
eligible for retry. The same process reuses the same strategy object, so any
mutation performed before the exception remains. After a process restart, the
caller constructs a new strategy object while the engine restores its own
checkpoint. Treating either behavior as implicit strategy persistence creates
different decisions for the same recovered event.

## Decision

Mutable strategy-instance state is not part of Librae's sim/live restart
contract. `Strategy.on_bar` must be retry-safe for an equivalent `Context`, and
restart-relevant trading state must be reconstructible from the supplied
context and causal input history. In particular, a strategy must not require an
instance counter or flag to remain synchronized with the engine's durable
`period_index`.

The engine does not copy or roll back a strategy object around `on_bar`, and it
does not serialize the object in `LiveRuntimeState`. Same-process retry keeps
mutations already made by a failing call; process restart begins with the
newly constructed strategy instance. Characterization tests make both halves
of this boundary explicit.

No generic snapshot/restore hooks are introduced. Versioned strategy state and
atomic persistence would require a concrete stateful strategy and failure
model; adding hooks without one would imply guarantees the current checkpoint
cannot provide.

## Consequences

Pure or history-derived strategies behave consistently across backtest,
same-event retry, and restart. Stateful strategies must first refactor their
decision state into causal inputs, or open a focused design issue describing
the required schema, compatibility policy, and atomic commit boundary before
being promoted to sim/live.
