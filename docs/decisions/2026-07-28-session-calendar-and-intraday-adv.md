# Session calendar and intraday ADV

Date: 2026-07-28

## Decision

Librae uses two separate time concepts:

- Every OHLCV `ts` is a timezone-aware UTC bar-start instant.
- `SymbolInfo.calendar_id` is the only source for mapping that instant to a
  trading-session label.

Standard exchange IDs are delegated to `exchange_calendars`. Librae adds
`24/7`, `XTAIFEX`, and `XTAIFEX_1725` for UTC-day crypto and the two supported
TAIFEX night-session openings. Shioaji's vendor epoch correction stays in the
adapter and is not reused as a trading calendar.

Intraday ADV sums observed bar volume by session, shifts by one complete
session, and averages exactly the configured number of completed sessions.
The engine maintains two independent capacities:

```text
available = min(
    bar participation cap - quantity filled in the current bar,
    ADV participation cap - quantity filled in the current session,
)
```

An intraday symbol using ADV must have a `calendar_id`; startup fails if it
does not. D1 remains compatible by treating each row as one session.

## Why

A timezone or UTC date cannot identify the trading date of a market whose
night session belongs to the following regular session. Conversely, shifting
all vendor timestamps to bar-end invents a duration for shortened or
session-ending bars. Explicit bar-start timestamps plus a separate session
label match the convention used by established event-driven engines while
keeping the implementation small.

Completed-bar filtering uses the calendar's real segment/session close when a
calendar is available. This matters for a TAIFEX daily bar whose prior-session
night open and regular-session close can span a weekend.

Session aggregation is sufficient for a cumulative ADV capacity limit. An
intraday volume profile would estimate how much daily liquidity should have
arrived by a particular minute, which is a different model and is not needed
because the existing current-bar participation cap already constrains local
fills.

## Boundaries

The calendar labels and resamples supplied observations; it does not generate
bars, schedules, strategy callbacks, or missing market data. Cross-market
baskets remain data-driven and sequential. The initial TAIFEX implementation
uses XTAI trading dates for its holiday-session set, so product-specific
exceptional closures must be filtered in upstream data.

This is a breaking contract clarification for custom adapters and ETL:
bar-end or naive timestamps must be normalized to UTC bar-start before entering
the engine.
