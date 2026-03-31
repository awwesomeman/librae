"""Core utility functions shared by backtest and live runtimes.

Provides:
- generate_run_id(): Deterministic-prefix run ID generation
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone


def generate_run_id(strategy: str, symbol: str) -> str:
    """Deterministic-prefix run_id: <strategy>-<symbol>-<ts>-<short_uuid>."""
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    short = uuid.uuid4().hex[:8]
    return f"{strategy}-{symbol}-{ts}-{short}".lower().replace(" ", "_")
