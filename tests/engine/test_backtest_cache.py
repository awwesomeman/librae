from __future__ import annotations

import pytest
from librae.backtest.cache import build_backtest_cache_key


def test_cache_key_is_stable_for_equal_identity() -> None:
    assert build_backtest_cache_key("config-a", "revision-a") == (
        build_backtest_cache_key("config-a", "revision-a")
    )


def test_cache_key_changes_with_config_or_revision() -> None:
    original = build_backtest_cache_key("config-a", "revision-a")

    assert build_backtest_cache_key("config-b", "revision-a") != original
    assert build_backtest_cache_key("config-a", "revision-b") != original


def test_missing_revision_disables_cache_identity() -> None:
    assert build_backtest_cache_key("config-a", None) is None


@pytest.mark.parametrize("revision", ["", " ", "\t"])
def test_empty_revision_is_rejected(revision: str) -> None:
    with pytest.raises(ValueError, match="backtest_revision"):
        build_backtest_cache_key("config-a", revision)


def test_revision_requires_config_hash() -> None:
    with pytest.raises(ValueError, match="config_hash"):
        build_backtest_cache_key(None, "revision-a")
