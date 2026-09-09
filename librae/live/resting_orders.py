"""Lifetime of a live intent that outlives the event which emitted it.

Mirrors the module-level helpers in ``librae/backtest/engine.py`` so a
deterministic runtime and a live one cannot drift on this sequence. Each
function takes the state it reads explicitly and returns what it changes;
``LiveTrader`` keeps thin methods that bind its own attributes.
"""

from __future__ import annotations

from collections.abc import Callable, Container, Mapping, MutableMapping
from datetime import datetime
from typing import TYPE_CHECKING

from librae.core.executor import RuntimeEvent
from librae.core.strategy import OrderIntent, PortfolioWeights, StrategyDecision
from librae.core.trading_calendar import require_resting_session_support, resting_session_label

from .interfaces import RuntimeEventCallback

if TYPE_CHECKING:
    from librae.config.symbols import SymbolInfo


def record_resting_since(
    decision: StrategyDecision,
    ts: datetime,
    resting_since: MutableMapping[str, datetime],
    *,
    primary_symbol: str,
) -> None:
    """Stamp the event an intent first rested on, once."""
    if isinstance(decision, PortfolioWeights):
        return
    for intent in decision:
        resting_since.setdefault(intent.symbol or primary_symbol, ts)


def replace_resting_intents(
    pending_decision: StrategyDecision,
    new_decision: StrategyDecision,
    ts: datetime,
    resting_since: MutableMapping[str, datetime],
    *,
    primary_symbol: str,
    on_runtime_event: RuntimeEventCallback | None,
) -> StrategyDecision:
    """Let a new decision cancel and replace an order still resting.

    Mirrors the backtest: ``gtc`` commits an order until it fills, so the
    strategy's next decision for that symbol is its way out. Only an intent
    that has actually rested is replaceable — one waiting for its symbol's
    first bar keeps the existing duplicate guard.

    Returns the pending decision that survives the replacement.
    """
    if not new_decision or isinstance(pending_decision, PortfolioWeights):
        return pending_decision
    replacing = (
        {intent.symbol or primary_symbol for intent in new_decision}
        if not isinstance(new_decision, PortfolioWeights)
        else None  # a whole-book target supersedes every resting order
    )
    live: list[OrderIntent] = []
    for intent in pending_decision:
        symbol = intent.symbol or primary_symbol
        resting = symbol in resting_since
        if resting and (replacing is None or symbol in replacing):
            resting_since.pop(symbol, None)
            if on_runtime_event:
                on_runtime_event(
                    RuntimeEvent(
                        ts=ts,
                        event_type="decision_skipped",
                        symbol=symbol,
                        detail={"reason": "resting_order_replaced"},
                    )
                )
            continue
        live.append(intent)
    return live


def validate_resting_sessions(
    decision: StrategyDecision,
    *,
    primary_symbol: str,
    instruments: Mapping[str, SymbolInfo],
    timeframe: str,
) -> None:
    """Fail on the emitting event, not mid-run, when a day limit has no
    session to expire against. This is engine expressibility, not a venue
    rule, so it belongs with the decision that requested it. Only calendar
    presence is checked — labelling this event would reject a decision
    emitted during another instrument's session."""
    if isinstance(decision, PortfolioWeights):
        return
    for intent in decision:
        if intent.time_in_force == "day" and intent.limit_price is not None:
            symbol = intent.symbol or primary_symbol
            try:
                require_resting_session_support(
                    calendar_id=instruments[symbol].calendar_id,
                    timeframe=timeframe,
                )
            except ValueError as exc:
                raise ValueError(f"{symbol}: {exc}") from exc


def prune_pending_submissions(
    pending_decision: StrategyDecision,
    resting_since: Mapping[str, datetime],
    *,
    primary_symbol: str,
) -> dict[str, datetime]:
    """Forget submissions whose intent is no longer pending."""
    if isinstance(pending_decision, PortfolioWeights):
        return {}
    still_pending = {intent.symbol or primary_symbol for intent in pending_decision}
    return {
        symbol: submitted_at
        for symbol, submitted_at in resting_since.items()
        if symbol in still_pending
    }


def session_of(symbol: str, ts: datetime, *, calendar_id: str, timeframe: str) -> object:
    """Session label a resting ``day`` order expires against."""
    try:
        return resting_session_label(ts, calendar_id=calendar_id, timeframe=timeframe)
    except ValueError as exc:
        raise ValueError(f"{symbol}: {exc}") from exc


def expire_resting_day_intents(
    pending_decision: StrategyDecision,
    ts: datetime,
    priced_symbols: Container[str],
    resting_since: MutableMapping[str, datetime],
    *,
    primary_symbol: str,
    session_of: Callable[[str, datetime], object],
    on_runtime_event: RuntimeEventCallback | None,
) -> StrategyDecision:
    """Drop resting ``day`` limits once their submitting session has ended.

    Mirrors the backtest so a deterministic runtime cannot drift on this
    sequence. Only a limit order can outlive an event, so a market order
    never consults the calendar.

    Returns the pending decision that survives expiry.
    """
    if isinstance(pending_decision, PortfolioWeights) or not pending_decision:
        return pending_decision
    live: list[OrderIntent] = []
    for intent in pending_decision:
        symbol = intent.symbol or primary_symbol
        submitted_at = resting_since.get(symbol)
        if (
            intent.time_in_force == "day"
            and intent.limit_price is not None
            and submitted_at is not None
            and symbol in priced_symbols
            and session_of(symbol, ts) != session_of(symbol, submitted_at)
        ):
            resting_since.pop(symbol, None)
            if on_runtime_event:
                on_runtime_event(
                    RuntimeEvent(
                        ts=ts,
                        event_type="decision_skipped",
                        symbol=symbol,
                        detail={"reason": "day_order_expired"},
                    )
                )
            continue
        live.append(intent)
    return live
