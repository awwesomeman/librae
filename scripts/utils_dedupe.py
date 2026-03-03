#!/usr/bin/env python3
from __future__ import annotations


def build_signal_key(setup_time: str, trigger_time: str) -> str:
    return f"{setup_time}::{trigger_time}"


def is_duplicate(last_key: str | None, key: str) -> bool:
    return last_key == key
