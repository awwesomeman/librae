"""Offline ccxt-shaped venue that serves history by ``since``, one page per request."""

from __future__ import annotations

import bisect
import time

BAR_MS = 5 * 60_000
FUNDING_MS = 8 * 3_600_000
PAGE = 1000


class PagedVenue:
    """Binance-like paging: ``since`` walks forward, ``None`` returns the newest rows.

    Candle ``limit`` is clamped to a page like ccxt's binance ``fetch_ohlcv``;
    a funding ``limit`` above a page is refused like the live endpoint. The
    newest bar is still forming. Settlements land 3 ms past the 8h grid.
    """

    def __init__(self, bars: int, *, funding_days: int = 400) -> None:
        now = int(time.time() * 1000)
        forming = now // BAR_MS * BAR_MS
        self.bar_ts = [forming - offset * BAR_MS for offset in range(bars - 1, -1, -1)]
        first_settlement = (now - funding_days * 86_400_000) // FUNDING_MS * FUNDING_MS
        self.funding_ts = [ts + 3 for ts in range(first_settlement, now - 3, FUNDING_MS)]
        self.ohlcv_calls: list[tuple[int | None, int]] = []
        self.funding_calls: list[tuple[int | None, int | None]] = []

    @staticmethod
    def _page(series: list[int], since: int | None, limit: int) -> list[int]:
        if since is None:
            return series[-limit:]
        start = bisect.bisect_left(series, since)
        return series[start : start + limit]

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None, params=None):
        self.ohlcv_calls.append((since, limit))
        rows = self._page(self.bar_ts, since, min(limit or 500, PAGE))
        return [[ts, 1.0, 1.0, 1.0, 1.0, 1.0] for ts in rows]

    def fetch_funding_rate_history(self, symbol=None, since=None, limit=None, params=None):
        self.funding_calls.append((since, limit))
        if limit is not None and limit > PAGE:
            raise RuntimeError('binanceusdm {"code":"99099990","errorData":"illegal params."}')
        rows = self._page(self.funding_ts, since, limit or 100)
        return [{"timestamp": ts, "fundingRate": 0.0001} for ts in rows]
