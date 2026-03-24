"""
Offline tests for InfluxDBActor helper functions.

Uses lightweight fake objects that match the field interface of
NautilusTrader's TradeTick and PositionChanged — no live NT
infrastructure required.
"""

import sys
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

# Make nautilus_lab importable from the tests directory
_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NL_ROOT = os.path.join(_WORKSPACE, "nautilus_lab")
if _NL_ROOT not in sys.path:
    sys.path.insert(0, _NL_ROOT)

from nautilus_lab.influx_actor import (
    build_tick_point,
    build_position_point,
    format_telegram_message,
)


# ---------------------------------------------------------------------------
# Fake objects — match field interface of TradeTick / PositionChanged
# ---------------------------------------------------------------------------

def _fake_price(value: float):
    """Minimal Price-alike that supports float()."""
    class _Price:
        def __init__(self, v): self._v = v
        def __float__(self): return float(self._v)
        def __str__(self): return str(self._v)
    return _Price(value)


def _fake_money(value: float, currency: str = "USDT"):
    """Minimal Money-alike that supports float()."""
    class _Money:
        def __init__(self, v, c): self._v = v; self._c = c
        def __float__(self): return float(self._v)
        def __str__(self): return f"{self._v} {self._c}"
    return _Money(value, currency)


def _fake_qty(value: float):
    class _Qty:
        def __init__(self, v): self._v = v
        def __float__(self): return float(self._v)
        def __str__(self): return str(self._v)
    return _Qty(value)


def make_fake_tick(
    instrument_id: str = "BTC/USDT.BINANCE",
    price: float = 50_000.0,
    size: float = 0.25,
    ts_event: int = 1_700_000_000_000_000_000,
) -> SimpleNamespace:
    """Fake TradeTick for offline testing."""
    return SimpleNamespace(
        instrument_id=instrument_id,
        price=_fake_price(price),
        size=_fake_qty(size),
        ts_event=ts_event,
    )


def make_fake_position_changed(
    instrument_id: str = "MXF.TAIFEX",
    side: str = "LONG",
    quantity: float = 3.0,
    avg_px_open: float = 21_500.0,
    unrealized_pnl: float = 900.0,
    realized_pnl: float = 0.0,
    ts_event: int = 1_700_000_001_000_000_000,
) -> SimpleNamespace:
    """Fake PositionChanged for offline testing."""
    return SimpleNamespace(
        instrument_id=instrument_id,
        side=side,
        quantity=_fake_qty(quantity),
        avg_px_open=_fake_price(avg_px_open),
        unrealized_pnl=_fake_money(unrealized_pnl),
        realized_pnl=_fake_money(realized_pnl),
        ts_event=ts_event,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBuildTickPoint(unittest.TestCase):

    def test_measurement_name(self):
        tick = make_fake_tick()
        pt = build_tick_point(tick)
        # Point._name is the measurement
        self.assertEqual(pt._name, "trade_ticks")

    def test_tags(self):
        tick = make_fake_tick(instrument_id="ETH/USDT.BINANCE")
        pt = build_tick_point(tick)
        tags = pt._tags
        self.assertEqual(tags.get("instrument_id"), "ETH/USDT.BINANCE")

    def test_fields_price_and_size(self):
        tick = make_fake_tick(price=48_000.5, size=1.5)
        pt = build_tick_point(tick)
        fields = pt._fields
        self.assertAlmostEqual(fields["price"], 48_000.5, places=4)
        self.assertAlmostEqual(fields["size"], 1.5, places=6)

    def test_timestamp_conversion(self):
        ts_ns = 1_700_000_000_000_000_000  # 2023-11-14T22:13:20 UTC
        tick = make_fake_tick(ts_event=ts_ns)
        pt = build_tick_point(tick)
        # Point._time should be the datetime derived from ts_ns
        expected_dt = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)
        self.assertEqual(pt._time, expected_dt)


class TestBuildPositionPoint(unittest.TestCase):

    def test_measurement_name(self):
        ev = make_fake_position_changed()
        pt = build_position_point(ev)
        self.assertEqual(pt._name, "position_changes")

    def test_tags(self):
        ev = make_fake_position_changed(instrument_id="MXF.TAIFEX", side="SHORT")
        pt = build_position_point(ev)
        self.assertEqual(pt._tags["instrument_id"], "MXF.TAIFEX")
        self.assertEqual(pt._tags["side"], "SHORT")

    def test_qty_field(self):
        ev = make_fake_position_changed(quantity=5.0)
        pt = build_position_point(ev)
        self.assertAlmostEqual(pt._fields["qty"], 5.0, places=6)

    def test_pnl_fields(self):
        ev = make_fake_position_changed(unrealized_pnl=1234.56, realized_pnl=50.0)
        pt = build_position_point(ev)
        self.assertAlmostEqual(pt._fields["unrealized_pnl"], 1234.56, places=2)
        self.assertAlmostEqual(pt._fields["realized_pnl"], 50.0, places=2)

    def test_none_pnl_omitted(self):
        ev = make_fake_position_changed()
        ev.unrealized_pnl = None
        ev.realized_pnl = None
        pt = build_position_point(ev)
        self.assertNotIn("unrealized_pnl", pt._fields)
        self.assertNotIn("realized_pnl", pt._fields)

    def test_timestamp_conversion(self):
        ts_ns = 1_700_000_001_000_000_000
        ev = make_fake_position_changed(ts_event=ts_ns)
        pt = build_position_point(ev)
        expected_dt = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)
        self.assertEqual(pt._time, expected_dt)


class TestFormatTelegramMessage(unittest.TestCase):

    def test_contains_instrument(self):
        ev = make_fake_position_changed(instrument_id="MXF.TAIFEX")
        msg = format_telegram_message(ev)
        self.assertIn("MXF.TAIFEX", msg)

    def test_contains_side_and_qty(self):
        ev = make_fake_position_changed(side="LONG", quantity=3.0)
        msg = format_telegram_message(ev)
        self.assertIn("LONG", msg)
        self.assertIn("3", msg)

    def test_contains_avg_px(self):
        ev = make_fake_position_changed(avg_px_open=21_500.0)
        msg = format_telegram_message(ev)
        self.assertIn("avg_px", msg)
        self.assertIn("21500", msg)

    def test_contains_unrealized_pnl(self):
        ev = make_fake_position_changed(unrealized_pnl=900.0)
        msg = format_telegram_message(ev)
        self.assertIn("upnl", msg)
        self.assertIn("900", msg)

    def test_no_avg_px_when_none(self):
        ev = make_fake_position_changed()
        ev.avg_px_open = None
        msg = format_telegram_message(ev)
        self.assertNotIn("avg_px", msg)

    def test_no_upnl_when_none(self):
        ev = make_fake_position_changed()
        ev.unrealized_pnl = None
        msg = format_telegram_message(ev)
        self.assertNotIn("upnl", msg)

    def test_message_is_concise(self):
        ev = make_fake_position_changed()
        msg = format_telegram_message(ev)
        # Should not be an essay
        self.assertLess(len(msg), 200)


if __name__ == "__main__":
    unittest.main()
