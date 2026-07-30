"""Pure backtest cache identity helpers."""

from __future__ import annotations

import hashlib


def normalize_backtest_revision(backtest_revision: str | None) -> str | None:
    """Validate and normalize a caller-owned code/data revision."""
    if backtest_revision is None:
        return None
    if not isinstance(backtest_revision, str):
        raise TypeError("backtest_revision must be a string or None")
    normalized = backtest_revision.strip()
    if not normalized:
        raise ValueError("backtest_revision must be a non-empty string or None")
    return normalized


def build_backtest_cache_key(
    config_hash: str | None,
    backtest_revision: str | None,
) -> str | None:
    """Return an opt-in cache key for one config and caller-owned revision."""
    revision = normalize_backtest_revision(backtest_revision)
    if revision is None:
        return None
    if not isinstance(config_hash, str) or not config_hash:
        raise ValueError("config_hash must be a non-empty string")

    identity = f"{config_hash}\0{revision}".encode()
    return hashlib.blake2b(identity, digest_size=16).hexdigest()
