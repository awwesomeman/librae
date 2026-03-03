#!/usr/bin/env python3
from __future__ import annotations
import json
from pathlib import Path


def rotate_if_oversize(path: str, max_mb: float = 10) -> None:
    p = Path(path)
    if p.exists() and p.stat().st_size > max_mb * 1024 * 1024:
        p.replace(p.parent / (p.name + '.1'))


def append_jsonl(path: str, obj: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('a', encoding='utf-8') as f:
        f.write(json.dumps(obj, ensure_ascii=False) + '\n')
