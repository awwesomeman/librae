"""A sim decides on, and records, only candles the venue has closed.

The venue can still be filling a candle after its close has passed on the
host, and the engine keeps the first version of a bar it receives: a candle
taken then is decided on and recorded short of its final volume, and the
complete one fetched a poll later changes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
from librae.core.run_config import ExecutionPolicy
from librae.core.strategy import Context, OrderIntent, Strategy
from librae.live.engine import LiveTrader

from tests.ccxt_standin import build_crypto_adapter
from tests.conftest import make_test_cfg

CLOSE = datetime(2026, 9, 21, 0, 15, tzinfo=UTC)
FINAL_VOLUME = 100.0


class _FillingVenue:
    """Returns the newest candles as the venue holds them at that moment."""

    def __init__(self) -> None:
        self.candles: list[list[float]] = []

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None, params=None):
        return self.candles[-(limit or len(self.candles)) :]

    def fetch_funding_rate_history(self, symbol=None, since=None, limit=None, params=None):
        return []


class _Hold(Strategy):
    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        return []


def _candle(start: datetime, volume: float) -> list[float]:
    return [int(start.timestamp() * 1000), 100.0, 101.0, 99.0, 100.0, volume]


def test_a_candle_the_venue_is_still_filling_is_neither_decided_nor_recorded(monkeypatch):
    clock = {"now": CLOSE + timedelta(milliseconds=500)}
    monkeypatch.setattr(
        "librae.brokers.crypto_adapter.time",
        SimpleNamespace(time=lambda: clock["now"].timestamp()),
    )
    venue = _FillingVenue()
    starts = [CLOSE - timedelta(minutes=5 * n) for n in range(4, 0, -1)]
    # Half a second past the close here, the venue has not started the next
    # candle and still lacks the last trades of this one.
    venue.candles = [_candle(start, FINAL_VOLUME) for start in starts[:-1]]
    venue.candles.append(_candle(starts[-1], 90.0))

    decided: list[tuple[pd.Timestamp, float]] = []
    recorded: list[tuple[datetime, float]] = []

    def features(frame: pd.DataFrame) -> pd.DataFrame:
        decided.append((frame.index[-1], float(frame["volume"].iloc[-1])))
        return frame

    trader = LiveTrader(
        _Hold(),
        features,
        config=make_test_cfg(
            symbols=["BTCUSDTPERP"],
            timeframe="M5",
            execution=ExecutionPolicy(max_bar_volume_participation_rate=None, warmup_periods=2),
            instrument_overrides={
                "BTCUSDTPERP": {
                    "instrument_type": "contract_perpetual",
                    "venue_symbol": "BTC/USDT:USDT",
                    "currency": "USDT",
                    "calendar_id": "24/7",
                }
            },
            symbol_cost_overrides={"BTCUSDTPERP": {"multiplier": 1.0}},
        ),
        adapter=build_crypto_adapter(venue, "binanceusdm"),
        on_bar=None,
        on_position_event=None,
        on_ohlcv=lambda _symbol, _tf, bar, ts: recorded.append((ts, bar["volume"])),
        on_heartbeat=None,
        on_signal_outcome=None,
        warmup_fetcher=None,
        clock=lambda: clock["now"],
    )

    trader._poll_cycle()
    clock["now"] = CLOSE + timedelta(seconds=30)
    venue.candles[-1] = _candle(starts[-1], FINAL_VOLUME)
    venue.candles.append(_candle(CLOSE, 5.0))
    trader._poll_cycle()

    last = starts[-1]
    assert (pd.Timestamp(last), FINAL_VOLUME) in decided
    assert all(volume == FINAL_VOLUME for _ts, volume in decided)
    assert (last, FINAL_VOLUME) in recorded
    assert all(volume == FINAL_VOLUME for _ts, volume in recorded)
