"""Thin FINRA data API client — one function, no business logic.

No API key needed (verified live 2026-07-22) — FINRA's public data API
(https://api.finra.org) is open, unlike FinMind/FRED which gate on a
token/key. POST + JSON body is FINRA's own query shape (compareFilters /
dateRangeFilters), not a REST convention this project chose.

Paginates internally via the ``record-total``/``record-offset`` response
headers (page size capped at ``record-max-limit`` = 5000, verified live
2026-07-22) — a caller's filter can legitimately return more than one page
(e.g. a long date range), and `get_factor()`'s coverage-tracked cache
assumes a fetcher answers its requested range *completely* (see
``us_chip.py``'s module docstring); a silently-truncated page would violate
that and get treated as fully covered forever. Kept here, not in each
concept file, since it's a FINRA API quirk, not domain logic.
"""
from __future__ import annotations

import json
import urllib.request

_BASE_URL = "https://api.finra.org/data/group"
_PAGE_LIMIT = 5000


def fetch(group: str, dataset: str, body: dict) -> list[dict]:
    """All rows for `group`/`dataset` (e.g. ``"otcMarket"``/
    ``"consolidatedShortInterest"``) matching `body`'s filters — FINRA's own
    filter shape (compareFilters/dateRangeFilters), passed through as-is
    except ``limit``/``offset``, which this function manages to page
    through the full result set.
    """
    url = f"{_BASE_URL}/{group}/name/{dataset}"
    rows: list[dict] = []
    offset = 0
    while True:
        page_body = {**body, "limit": _PAGE_LIMIT, "offset": offset}
        req = urllib.request.Request(
            url,
            data=json.dumps(page_body).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            page = json.loads(resp.read())
            total = int(resp.headers.get("record-total", len(page)))
        rows.extend(page)
        offset += len(page)
        if not page or offset >= total:
            break
    return rows
