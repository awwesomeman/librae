"""Lifetime of an order intent that outlives the event which emitted it.

Backtest and simulation both keep a limit intent resting across events. The
rules for when one expires and when a new decision replaces it live here once,
so the two runtimes cannot drift. Each engine keeps its own record of when an
intent first rested and passes a lookup for it; the functions return the
intents that survive and the symbols that did not, which the caller audits.
"""

from __future__ import annotations

from collections.abc import Callable, Container, Iterable
from datetime import datetime

from librae.core.strategy import OrderIntent, PortfolioWeights, StrategyDecision
from librae.core.trading_calendar import require_resting_session_support, resting_session_label

# When the intent for a symbol first rested, or None while it has not: a
# decision is emitted on one event and first executable on the next, so its
# lifetime starts on the first event it was eligible on.
RestingSince = Callable[[str], datetime | None]


def _expires_with_session(intent: OrderIntent) -> bool:
    """Whether ``intent`` rests until its session ends: a ``day`` limit.

    Only a limit order can outlive an event, so a market order never consults
    the calendar and a run that rests nothing pays no calendar cost.
    """
    return intent.time_in_force == "day" and intent.limit_price is not None


def require_resting_sessions(
    decision: StrategyDecision,
    *,
    primary_symbol: str,
    calendar_id_of: Callable[[str], str | None],
    timeframe: str,
) -> None:
    """Fail on the emitting event, not mid-run, when a day limit has no
    session to expire against.

    This is engine expressibility, not a venue rule, so it belongs with the
    decision that requested it. Only calendar presence is checked: on a union
    timeline the emitting event can lie outside this symbol's session, which
    is not an error.
    """
    if isinstance(decision, PortfolioWeights):
        return
    for intent in decision:
        if _expires_with_session(intent):
            symbol = intent.symbol or primary_symbol
            try:
                require_resting_session_support(
                    calendar_id=calendar_id_of(symbol),
                    timeframe=timeframe,
                )
            except ValueError as exc:
                raise ValueError(f"{symbol}: {exc}") from exc


def resting_session_of(
    symbol: str,
    ts: datetime,
    *,
    calendar_id: str | None,
    timeframe: str,
) -> object:
    """Session label a resting ``day`` order expires against."""
    try:
        return resting_session_label(ts, calendar_id=calendar_id, timeframe=timeframe)
    except ValueError as exc:
        raise ValueError(f"{symbol}: {exc}") from exc


def expire_day_intents(
    intents: Iterable[OrderIntent],
    ts: datetime,
    priced_symbols: Container[str],
    *,
    resting_since: RestingSince,
    primary_symbol: str,
    session_of: Callable[[str, datetime], object],
) -> tuple[list[OrderIntent], list[str]]:
    """Drop resting ``day`` limits once the session they rested in has ended.

    The boundary is the instrument's own trading session, not a wall-clock
    day, so a venue whose session spans midnight keeps its orders alive across
    it. Expiry is judged only on an event where the symbol has a bar
    (``priced_symbols``): on a union timeline another instrument's bar can
    fall outside this one's session entirely, which is not a boundary its
    orders crossed.

    Returns the intents that survive and the symbols that expired, in order.
    """
    kept: list[OrderIntent] = []
    expired: list[str] = []
    for intent in intents:
        symbol = intent.symbol or primary_symbol
        since = resting_since(symbol) if _expires_with_session(intent) else None
        if (
            since is not None
            and symbol in priced_symbols
            and session_of(symbol, ts) != session_of(symbol, since)
        ):
            expired.append(symbol)
        else:
            kept.append(intent)
    return kept, expired


def replace_resting_intents(
    intents: Iterable[OrderIntent],
    new_decision: StrategyDecision,
    *,
    resting_since: RestingSince,
    primary_symbol: str,
) -> tuple[list[OrderIntent], list[str]]:
    """Let a new decision cancel and replace an order still resting.

    ``gtc`` commits an order until it fills or the run ends, so without this
    the strategy has no way out and its next decision for that symbol would
    collide with the resting one. Standard cancel/replace: the newer decision
    wins and the replaced order is audited. A whole-book ``PortfolioWeights``
    target replaces every resting order. Only an intent that has rested is
    replaceable: one waiting for its symbol's first bar has not been offered
    to the market yet and keeps the duplicate-intent guard.

    Returns the intents that survive and the symbols replaced, in order.
    """
    if not new_decision:
        return list(intents), []
    replacing = (
        None
        if isinstance(new_decision, PortfolioWeights)
        else {intent.symbol or primary_symbol for intent in new_decision}
    )
    kept: list[OrderIntent] = []
    replaced: list[str] = []
    for intent in intents:
        symbol = intent.symbol or primary_symbol
        if resting_since(symbol) is not None and (replacing is None or symbol in replacing):
            replaced.append(symbol)
        else:
            kept.append(intent)
    return kept, replaced
