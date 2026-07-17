"""Shared adapter metadata and credential loading.

Concrete adapters (CryptoAdapter, ShioajiAdapter, etc.) are sync and
duck-typed — they implement ``fetch_ohlcv``/``place_order``/``get_position``
directly, matched by shape rather than an ABC. This module only holds the
two pieces every adapter actually shares: static metadata and env-var
credential loading.

(An async ABC layer — MarketDataAdapter/OrderAdapter/AccountAdapter plus
canonical L1Quote/TradeTick/Bar/Order/Fill/Position types — used to live
here for a future real-time/multi-venue design, but no adapter ever
implemented it; removed to avoid a second, incompatible "OrderAdapter"
next to the real one in librae/live/executor.py.)
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from typing import Self


# ---------------------------------------------------------------------------
# Canonical data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterInfo:
    """Static metadata about an adapter instance."""

    adapter_id: str
    venue: str
    market_type: str
    schema_version: str = "1.0.0"


# ---------------------------------------------------------------------------
# Credential config
# ---------------------------------------------------------------------------


@dataclass
class CredentialConfig:
    """Base credential config with env-var loading.

    Subclass and add fields for each venue.  Call ``from_env()`` to
    populate fields from environment variables automatically.

    Env-var mapping convention: ``{PREFIX}_{FIELD_UPPER}``.
    E.g. ``BINANCE_API_KEY`` for field ``api_key`` with prefix ``BINANCE``.
    """

    @classmethod
    def from_env(cls, prefix: str, **overrides: str) -> Self:
        """Build credentials from environment variables.

        For each dataclass field, looks up ``{prefix}_{FIELD_UPPER}``
        in ``os.environ``.  Explicit *overrides* take precedence.
        """
        _sentinel = object()
        kwargs: dict = {}
        for f in dataclasses.fields(cls):
            override = overrides.get(f.name, _sentinel)
            if override is not _sentinel:
                kwargs[f.name] = override
            else:
                env_val = os.environ.get(f"{prefix}_{f.name.upper()}")
                if env_val is not None:
                    kwargs[f.name] = env_val
        return cls(**kwargs)
