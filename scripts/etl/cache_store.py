"""File-based cache for ETL data payloads (e.g. Binance klines rows).

Public API
----------
build_cache_key(namespace, params_dict) -> str
    Deterministic hex key from namespace + sorted params.

load_cache(cache_dir, key, ttl_seconds) -> list | None
    Return cached payload if fresh, else None.

save_cache(cache_dir, key, payload)
    Atomically persist payload via tmp-file + rename.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from typing import Any


def build_cache_key(namespace: str, params_dict: dict[str, Any]) -> str:
    """Return a deterministic 16-char hex key for *namespace* + *params_dict*."""
    canonical = json.dumps({"ns": namespace, **params_dict}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def load_cache(cache_dir: str, key: str, ttl_seconds: float) -> list | None:
    """Return payload list if cache file exists and is within *ttl_seconds*, else None."""
    path = os.path.join(cache_dir, f"{key}.json")
    try:
        mtime = os.path.getmtime(path)
    except FileNotFoundError:
        return None
    if time.time() - mtime > ttl_seconds:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and "payload" in data:
            return data["payload"]
    except (json.JSONDecodeError, OSError):
        return None
    return None


def save_cache(cache_dir: str, key: str, payload: list) -> None:
    """Atomically write *payload* to *cache_dir*/*key*.json via tmp+rename."""
    os.makedirs(cache_dir, exist_ok=True)
    target = os.path.join(cache_dir, f"{key}.json")
    data = json.dumps({"payload": payload}, separators=(",", ":"))
    # Write to a sibling tmp file then rename for atomicity.
    fd, tmp_path = tempfile.mkstemp(dir=cache_dir, prefix=f"_{key}_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
