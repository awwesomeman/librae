from __future__ import annotations

import pandas as pd
import pytest
from librae.core.executor import _volume_fill_limit
from librae.core.liquidity import calculate_lagged_adv


def test_lagged_adv_excludes_current_daily_bar() -> None:
    volume = pd.Series([100.0, 200.0, 300.0, 1_000.0])

    result = calculate_lagged_adv(volume, lookback_sessions=3)

    assert result.iloc[:3].isna().all()
    assert result.iloc[3] == pytest.approx(200.0)


@pytest.mark.parametrize("invalid_volume", [float("nan"), float("inf"), -1.0])
def test_lagged_adv_rejects_invalid_volume_history(invalid_volume: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        calculate_lagged_adv(pd.Series([100.0, invalid_volume]), lookback_sessions=1)


def test_volume_limit_uses_tightest_bar_and_adv_budget() -> None:
    limit = _volume_fill_limit(
        "AAA",
        0.10,
        1_000.0,
        max_adv_participation_rate=0.02,
        lagged_adv=2_000.0,
        used_quantity=15.0,
    )

    assert limit == pytest.approx(25.0)


def test_adv_limit_rejects_fill_until_full_history_exists() -> None:
    assert (
        _volume_fill_limit(
            "AAA",
            None,
            1_000.0,
            max_adv_participation_rate=0.02,
            lagged_adv=None,
        )
        == 0.0
    )
