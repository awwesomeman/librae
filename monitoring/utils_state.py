#!/usr/bin/env python3
from __future__ import annotations
import json
from pathlib import Path


def load_json_state(path: str) -> dict:
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text(encoding='utf-8'))
    return {}


def save_json_state(path: str, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
