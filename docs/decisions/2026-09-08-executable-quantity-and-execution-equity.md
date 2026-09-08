# Normalize executable quantity before execution-time risk checks

Date: 2026-09-08
Status: Accepted

## Context

The deterministic executor accepted arbitrary positive floating-point
quantities while live broker adapters applied venue size increments and
minimums immediately before submission. A backtest could therefore fill a
fractional futures contract, stock lot, or unsupported crypto precision that
the equivalent live order would round down or reject. Quantity changes made by
cash, position-notional, or liquidity caps could recreate the same problem
after an initially valid request.

`RiskPolicy.max_position_weight` had a separate causal mismatch: it converted
the weight to a notional cap using the previous event's equity even though
current execution marks were already available. A gap in another holding could
therefore size a new position against stale, higher equity.

## Decision

`SymbolInfo` owns two optional common execution facts:

- `quantity_step` is the smallest shared executable increment;
- `min_quantity` is the smallest shared executable order.

The core normalizer uses decimal arithmetic and rounds positive quantities
toward zero. Backtest, simulation, and live planning apply it to explicit
`OrderIntent` quantities, quantities derived from `PortfolioWeights`, all-cash
sizing, risk/liquidity/cash clamps, protective exits, and forced liquidation
before costs or final constraint validation. A quantity below the minimum is
not promoted upward. It is skipped, or rejects a grouped fill-or-kill unit.

Grouped explicit legs must retain one common normalized scale. If increments
would change their relative ratios, the group is rejected before state is
mutated or a broker request is submitted. This is a local planning guarantee;
it does not imply cross-venue transactionality.

Broker preparation remains the final venue authority. It may reduce a shared
quantity for a more specific current rule but may not increase it. The prepared
quantity is replayed through the shared normalizer and risk checks before
checkpointing and submission. Exchange-discovered crypto precision, changing
minimum notionals, and other venue-specific rules remain in adapters rather
than being frozen into the library registry. A run that needs deterministic
parity supplies its stable common step/minimum through
`instrument_overrides`.

The per-position notional cap is derived from equity marked at the execution
event using confirmed cash, current positions, and the same causal marks used
by exposure validation. The cap still applies only to exposure-increasing
fills; closes and reductions remain eligible when an existing position is
already over its limit.

## Consequences

Target weights can retain cash when the exact target is not representable by
the instrument increment. Volume or risk capacity smaller than one executable
unit produces no fill rather than a fractional fill. Built-in TAIFEX futures
and the built-in US equity declare whole units; dynamic crypto rules remain
explicit configuration plus adapter validation.

The optional `symbols` reference table mirrors the two new `SymbolInfo` fields.
As with other schema changes in this repository, an existing database must be
recreated or migrated by the operator before the updated writer is used.
