# Grouped close legs may omit quantity; unfillable-as-written groups raise

Date: 2026-09-07
Status: Accepted

## Context

Atomic grouped execution in backtest/sim (the `atomic_groups` path of
issue #107) stages each group against copied state and commits only if every
leg filled its stated quantity. Two consequences surfaced in review:

- A `close` leg is capped at the position's size, so `close(B, 10)` on a
  position of 8 fills 8 — a complete exit — and was still judged a partial
  fill. Because every grouped leg had to carry an explicit quantity, a
  strategy had no way to say "all of it", and a pair whose leg had been
  reduced by a volume-capped stop could never be exited under its group id
  again. The failure was a `group_unfillable` runtime event, so nothing
  surfaced until someone read the event log.
- The first implementation pre-checked prices and quantities inside the
  unit loop and turned a leg with no executable price into the same skip
  event. That made the `lost pricing ... cannot fill some legs and not others`
  raise that [`2026-08-05`](2026-08-05-grouped-decisions-no-engine-side-waiting.md)
  installed unreachable on the atomic path — reversing an accepted decision
  without saying so.

Mainstream practice, checked before deciding:

- Exchange-listed multi-leg instruments (CME Globex and Eurex spreads, listed
  option combos) fill partially only in whole ratio units — never one leg
  ahead of another. The invariant is ratio preservation, not all-or-nothing.
- Perpetual-futures venues (Binance USD-M, Bybit, OKX) expose `reduceOnly`
  and `closePosition` flags precisely because "close what I hold" cannot be
  stated safely as a fixed number when the position can change underneath
  the order. `closePosition` ignores the quantity field outright.
- Fill-or-kill and all-or-none are single-order qualifiers; basket and list
  orders at brokers are independent legs with no venue atomicity.

The live preflight (issue #109) already enforces a per-group ratio guard on
adapter-prepared quantities, so a quantity-less close is consistent with the
live side rather than a backtest-only convenience.

## Decision

- `validate_strategy_decision` accepts a grouped `close` intent with
  `quantity=None`, meaning the whole position. Entry legs (`long`/`short`)
  still require an explicit quantity: without one they size from available
  cash, which is not deterministic across legs.
- The atomic grouped execution path proves every group fillable as
  written *before* the first unit in the decision executes, and raises
  `ValueError` if a leg has no executable price, closes a symbol with no open
  position, or closes more than is held. Preflighting before the loop matters
  because ungrouped units commit straight into the caller's book; a raise
  mid-loop would leave positions moved while the cash delta is lost with the
  exception. Reading position sizes ahead of the loop is sound because a
  decision holds at most one intent per symbol.
- A group that passes preflight and is then only partly filled by the venue
  simulation — cash, bar volume, or ADV budget — is still rolled back with no
  mutation and recorded as a `group_unfillable` runtime event. Those are
  market conditions, not decision errors, and match the `insufficient_cash`
  and `volume_capped` idioms for ungrouped intents.
- The in-loop `fully_filled` test compares each fill to the size preflight
  established (the stated quantity, or the position for a quantity-less
  close), not to the intent's quantity field.

This keeps the loud/quiet split of `2026-08-05`: what the strategy could
have known at decision time raises; what only the venue decides is an
event.

## Consequences

Strategies that exit a pair should write `OrderIntent(action="close",
symbol=..., group_id=...)` with no quantity. A stated close quantity larger
than the position is now an error on the first bar it happens, where it was
previously a silent event and a permanently stuck pair. A leg that loses its
price between decision and execution raises again, as it did before #107.

Two #107 tests that asserted the silent events for these cases were
rewritten to assert the raise and the absence of mutation. No compatibility
shim, consistent with
[`2026-07-28-strategy-decision-execution-naming.md`](2026-07-28-strategy-decision-execution-naming.md).

## References

- [`2026-08-05-grouped-decisions-no-engine-side-waiting.md`](2026-08-05-grouped-decisions-no-engine-side-waiting.md)
- [`2026-07-28-strategy-decision-execution-naming.md`](2026-07-28-strategy-decision-execution-naming.md)
- Binance USD-M Futures REST API, New Order — the `reduceOnly` and
  `closePosition` parameters
- CME Globex spread instruments and listed option combos — partial fills
  occur in whole ratio units
