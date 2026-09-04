# Make rebalance residual slicing explicit and bounded

Date: 2026-09-04
Status: Accepted

## Context

A `PortfolioWeights` target can fill only part of its requested quantity when
bar-volume or lagged-ADV participation limits bind. Silently discarding that
remainder understates implementation shortfall, while forcing every strategy
to carry orders changes established one-bar behavior and is unsuitable for
strategies that intentionally refresh their target every event.

## Decision

`ExecutionPolicy.rebalance_residual_policy` is a typed, backtest-only opt-in:

- `discard` preserves the established one-shot partial/drop compatibility path;
- `fail` stages the complete attempt and raises without committing position or
  ADV mutations if any requested quantity remains;
- `defer_all` carries explicit residual quantities but makes no progress on an
  event where any residual symbol lacks a tradable side or liquidity budget;
- `defer_symbols` carries the same quantities and lets independently executable
  symbols progress.

Deferred policies require a positive `max_rebalance_delay_bars`. At first
eligibility, causal point-in-time marks freeze equity and each signed target
notional. A symbol with a fresh execution price is immediately converted to
explicit reduction/addition quantities; a missing-market symbol retains an
unresolved notional leg and is converted using its first later fresh price.
Stale marks therefore value existing holdings but never manufacture a fill or
an order quantity. `defer_symbols` can progress resolved symbols while
`defer_all` waits for the whole book. Once resolved, quantities are not resized
from moving weights: each symbol carries its actual unfilled quantity.

Reductions execute before additions on every event, and additions are
cash-scaled only against cash actually present after filled reductions. When
no reduction can release more cash, common cash scaling defines the final
achievable allocation; its cost-driven tail is audited as cancelled rather
than mislabeled as a liquidity residual. If a reduction is still pending, the
addition remainder stays live so later realized proceeds can fund it. Under
`fail`, a target that exceeds the
structural position-notional constraint is atomically rejected. Deferred modes
execute the permitted quantity and explicitly audit/cancel the excess instead
of mislabeling a permanent risk constraint as a retryable liquidity residual.

A newer complete target replaces residual state using actual current positions.
It does not reset the lifetime budget, so repeatedly emitting targets cannot
create an unbounded order. All retained-policy slices are staged, so a later
exception cannot leak an earlier reduction or ADV mutation. Residual and
supersession events aggregate to one durable row per timestamp/event
type/symbol, with phase breakdowns where necessary. Resolved legs contain
requested, filled, and remaining quantities. Unresolved legs instead report an
explicit `awaiting_fresh_price` state and target-allocation notionals; they
never invent a quantity. Protective exits own risk priority: a triggered stop,
take-profit, or liquidation cancels the same symbol's residual target, while a
volume-limited stop/liquidation remains a pending market exit.

## Consequences

Sparse panels and side-level `can_buy`/`can_sell` facts remain the source of
execution availability. Missing bars and unavailable sides defer rather than
manufacture fills. Bar-volume and ADV accounting remain owned by the existing
executor, including cumulative per-bar and per-session budgets.

This state is intentionally in-memory and backtest-only. Broker order IDs,
cancel/replace behavior, durable recovery, and wall-clock scheduling remain
live execution concerns and are not implied by this policy.
