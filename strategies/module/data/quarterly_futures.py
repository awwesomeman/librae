"""Binance UM quarterly delivery futures — the third contract type
(spot/perpetual already covered by ``ohlcv.py``/``open_interest.py``).
Same six OI/positioning metrics as ``open_interest.py``, same archive
(``data.binance.vision``), just a dated contract symbol (e.g.
``"BTCUSDT_260925"``) instead of the perpetual ``"BTCUSDT"`` — reuses
``open_interest._metrics_column_fetcher`` directly rather than duplicating
the day-by-day zip-download loop a third time.

Caller must pass the literal current-quarter contract symbol — no roll/
continuous-contract resolution here yet (deferred; picking the right
contract as it rolls each quarter is on the caller for now). Verified live
2026-07-23: dated symbols exist back to ``BTCUSDT_211231``; expired
contracts keep their full archive after delivery.

    df = get_factor("BTCUSDT_260925", "quarterly_open_interest", start="2026-01-01")
"""
from __future__ import annotations

from strategies.module.data.factors import register_factor_fetcher
from strategies.module.data.open_interest import _COLUMNS, _SOURCE, _metrics_column_fetcher

for _factor_name, _raw_column in _COLUMNS.items():
    register_factor_fetcher(
        f"quarterly_{_factor_name}", _metrics_column_fetcher(_raw_column),
        source=_SOURCE, frequency="M5", instrument_type="contract_quarterly",
    )
