"""TAIFEX contract rules that bar data cannot show.

Requires the ``calendars`` extra, like every TAIFEX session lookup.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import cache

from librae.core.trading_calendar import TAIFEX_INDEX_CALENDAR, session_bounds
from librae.core.utils import validate_contract_month


@cache
def taifex_final_settlement_day(contract_month: str) -> date:
    """Final settlement day of a TAIFEX monthly futures contract.

    The contract specifications for the TAIEX futures and their mini and micro
    contracts make the last trading day the third Wednesday of the delivery
    month and the final settlement day the same day
    (https://www.taifex.com.tw/cht/2/tX). When TAIFEX is closed on that
    Wednesday, settlement moves to the next trading session: the January 2023
    contract settled on 2023-01-30 (https://www.taifex.com.tw/cht/5/futIndxFSP).
    Sessions come from the ``XTAIFEX`` calendar, which adds closures the
    upstream calendar lacks.

    Args:
        contract_month: Delivery month as ``"YYYYMM"``.
    """
    if not isinstance(contract_month, str):
        raise TypeError("contract_month must be a 'YYYYMM' string")
    validate_contract_month(contract_month)
    first = date(int(contract_month[:4]), int(contract_month[4:]), 1)
    day = first + timedelta(days=(2 - first.weekday()) % 7 + 14)
    while True:
        try:
            session_bounds(day, TAIFEX_INDEX_CALENDAR)
        except ValueError as exc:
            # Reached only once session_bounds has loaded exchange_calendars.
            from exchange_calendars.errors import DateOutOfBounds

            if isinstance(exc, DateOutOfBounds):
                raise
            day += timedelta(days=1)
            continue
        return day
