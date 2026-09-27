"""Offline ccxt-shaped venue that serves history by ``since``, one page per request."""

from __future__ import annotations

import bisect
from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd

BAR_MS = 5 * 60_000
FUNDING_MS = 8 * 3_600_000
PAGE = 1000
# Mid-bar, so the newest bar is still forming.
NOW = pd.Timestamp("2026-01-01T00:02:30Z")
NOW_MS = NOW.value // 1_000_000


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.to_pydatetime().astimezone(tz or UTC)


class PagedVenue:
    """Binance-like paging: ``since`` walks forward, ``None`` returns the newest rows.

    Candle ``limit`` is clamped to a page like ccxt's binance ``fetch_ohlcv``,
    and an ``inverse`` market also ends each request at ``since + limit`` bars
    as it does. A funding ``limit`` above a page is refused like the live
    endpoint. Settlements land 3 ms past the 8h grid.

    Building one pins the clocks the adapter and ``drop_incomplete`` read to
    ``NOW``, so a bar boundary passing mid-test changes nothing.
    """

    def __init__(
        self, monkeypatch, bars: int, *, funding_days: int = 400, inverse: bool = False
    ) -> None:
        monkeypatch.setattr(
            "librae.brokers.crypto_adapter.time", SimpleNamespace(time=lambda: NOW_MS / 1000)
        )
        monkeypatch.setattr("librae.brokers.base.datetime", _FrozenDatetime)
        forming = NOW_MS // BAR_MS * BAR_MS
        self.bar_ts = [forming - offset * BAR_MS for offset in range(bars - 1, -1, -1)]
        first_settlement = (NOW_MS - funding_days * 86_400_000) // FUNDING_MS * FUNDING_MS
        self.funding_ts = [ts + 3 for ts in range(first_settlement, NOW_MS - 3, FUNDING_MS)]
        self.inverse = inverse
        self.ohlcv_calls: list[tuple[int | None, int | None]] = []
        self.funding_calls: list[tuple[int | None, int | None]] = []

    @staticmethod
    def _page(series: list[int], since: int | None, limit: int) -> list[int]:
        if since is None:
            return series[-limit:]
        start = bisect.bisect_left(series, since)
        return series[start : start + limit]

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None, params=None):
        self.ohlcv_calls.append((since, limit))
        limit = min(limit or 500, PAGE)
        rows = self._page(self.bar_ts, since, limit)
        if self.inverse and since is not None:
            rows = [ts for ts in rows if ts < since + limit * BAR_MS]
        return [[ts, 1.0, 1.0, 1.0, 1.0, 1.0] for ts in rows]

    def fetch_funding_rate_history(self, symbol=None, since=None, limit=None, params=None):
        self.funding_calls.append((since, limit))
        if limit is not None and limit > PAGE:
            raise RuntimeError('binanceusdm {"code":"99099990","errorData":"illegal params."}')
        rows = self._page(self.funding_ts, since, limit or 100)
        return [{"timestamp": ts, "fundingRate": 0.0001} for ts in rows]
