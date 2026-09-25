"""Every bundled example's prepare_signals must not read later bars."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

import pandas as pd
import pytest
from examples.minimum_variance import run as minimum_variance
from examples.minimum_variance.strategy import prepare_signals as prepare_minimum_variance
from examples.multi_leg_spread import run as multi_leg_spread
from examples.multi_leg_spread.strategy import prepare_signals as prepare_spread
from examples.simple_sma import run as simple_sma
from examples.simple_sma.strategy import prepare_signals as prepare_sma
from examples.topk_selection import run as topk_selection
from examples.topk_selection.strategy import prepare_signals as prepare_topk
from librae.testing import validate_feature_causality


@pytest.mark.parametrize(
    "feature_fn,make_bars",
    [
        (prepare_sma, simple_sma._fetch_ohlcv),
        (
            prepare_minimum_variance,
            lambda: minimum_variance._make_panel(["LOW_VOL", "MID_VOL", "HIGH_VOL"], "XNYS"),
        ),
        (
            prepare_topk,
            lambda: topk_selection._make_panel(["ALPHA", "BETA", "GAMMA", "DELTA"], "XNYS"),
        ),
        (
            partial(prepare_spread, near_symbol="SPREAD_NEAR", far_symbol="SPREAD_FAR"),
            lambda: multi_leg_spread._make_panel(["SPREAD_NEAR", "SPREAD_FAR"], "XNYS"),
        ),
    ],
    ids=["simple_sma", "minimum_variance", "topk_selection", "multi_leg_spread"],
)
def test_example_features_are_causal(
    feature_fn: Callable[[pd.DataFrame], pd.DataFrame],
    make_bars: Callable[[], pd.DataFrame],
) -> None:
    validate_feature_causality(feature_fn, make_bars(), cuts=10)
