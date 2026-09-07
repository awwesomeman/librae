# Intrabar ordering ambiguity defers the fill instead of aborting the run

Date: 2026-09-07
Status: Accepted

## Context

Two engine rules, each defensible alone, collided once both existed.

Issue #101 gave `PortfolioWeights` a bounded whole-book deferral: when some
symbol has no executable price, the whole target waits for a later bar,
signalled by an exception the engine loop catches.

Issue #112 made the engine fail closed on intrabar ambiguity: OHLCV cannot
establish whether a stop or take-profit triggered before or after a fill
priced at the bar's close, high, or low, so a pending decision overlapping a
triggered protection raised rather than guessing. It raised a bare
`ValueError`, which the deferral's `except` clause could not see.

Reproduced on a merge of both branches: a bar where the rebalance would have
deferred, carrying a position whose stop genuinely triggered, aborted the
whole run — and the message named stop ordering, so the reader could not tell
that a deferrable rebalance was the real trigger.

Surveying how bar-level engines handle the ambiguity itself:

- **backtesting.py** meets this exact case and postpones. Its warning reads
  "Since we can't assert the precise intra-candle price movement, the affected
  SL/TP order will instead be executed on the next (matching) price/bar,
  making the result (of this trade) somewhat dubious." It is a `UserWarning`,
  not an exception.
- **vectorbt** forbids resolving a stop on the entry bar by construction:
  "Stop signal cannot be processed at the same bar as the entry signal," and
  its maintainer's advice for the ambiguous case is to postpone to the next
  bar.
- **NautilusTrader** documents bar execution as "a plausible intrabar path
  rather than reconstructing the original trades," and exposes the ordering
  as a configuration flag, naming this scenario — "when both a protective stop
  and a profit target lie inside the same bar" — as where it matters.
- **QuantConnect LEAN** resolves it silently by always assuming the worse of
  the stop and the current price.
- **Backtrader** applies an undocumented fixed convention, and its own docs
  describe same-bar close fills as knowingly unsound convenience rather than a
  condition to refuse.

No engine, doc, or maintainer comment found in that survey aborts a run on
intrabar ambiguity. Librae's rule was stricter than all five.

The observation that settles it: deferring the fill does not dodge the
ambiguity, it removes it. Ambiguity requires the protection and the decision
to land on one bar. Push the decision to a later bar and the protection is
alone on this one, executing at its own price — the conservative,
protection-first resolution.

## Decision

- Deferrable execution conditions become a category. `ExecutionUnavailableError`
  is the base, carrying the blocked symbols; the price-unavailable case and
  the new `AmbiguousBarOrderingError` are its subclasses, each owning the
  wording of why. The engine catches the base, so a further reason for "not on
  this bar" reaches the same bounded retry without the loop learning about it.
- The ambiguity guard raises that subclass. Where a deferral budget exists the
  fill moves to a later bar; where none does — including the zero default —
  the caller still sees the raise, so fail-closed remains the default and no
  existing configuration changes behaviour.
- Exhausting the bound stays fatal. That is the genuinely unrecoverable state,
  and the one condition none of the surveyed engines has an opinion about.
- The deferral log and the bound-exhaustion message quote the raised instance
  instead of asserting a cause they no longer know.

Mature trading libraries express the recoverable/fatal split the same
structural way: freqtrade's `TemporaryError` and `RetryableOrderError` against
`OperationalException` under one `FreqtradeException` base, ccxt's retryable
`NetworkError` subtree under `BaseError`, and backtrader's loop-handled
`StrategySkipError` under `BacktraderError`. Zipline shows the in-loop
variant, raising a dedicated `LiquidityExceeded` deep in the slippage model
and catching it in `simulate` as a `break` that leaves the order open for the
next bar.

## Consequences

A run configured with `max_rebalance_delay_bars` no longer aborts when a
rebalance bar also carries a triggered protection; the protection executes
that bar and the target fills once its budget allows. A run on the zero
default behaves exactly as before.

Librae still refuses to invent an intrabar path, which is the part of issue
#112 that matters — it declines to fill on an ambiguous bar at all, rather
than choosing a convention as LEAN and Backtrader do or exposing a flag as
NautilusTrader does. That is the deliberate divergence: the ambiguity is
answered by waiting, not by a heuristic.

## References

- [backtesting.py, contingent-order same-bar warning](https://github.com/kernc/backtesting.py/issues/119)
- [NautilusTrader, bar execution](https://nautilustrader.io/docs/latest/concepts/backtesting/bar-execution/)
- vectorbt portfolio enums, `StopEntryPrice`/`StopExitPrice` ordering rules
- freqtrade `exceptions.py`, ccxt `errors.py`, backtrader `errors.py`,
  zipline `slippage.py` — exception-hierarchy precedent
- Issues #101 and #112
