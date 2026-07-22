"""Central factor formula catalog — one place to look up how every factor is
computed.

Naming convention: ``[<tf_prefix>_]<family>_<stat>[_p<window>]``.

- ``family``: fixed vocabulary (``mom``, ``rsi``, ``vol``, ``z``, ``corr``,
  ``regime``, ``relmom``) — extend the vocabulary when a genuinely new family
  shows up, don't coin a one-off word per factor.
- ``stat`` (optional): transform applied, e.g. ``demeaned``, ``rel``.
- ``window``: always ``p<N>`` — N periods (bars) of whatever timeframe the
  input ``df`` is in. Never bake a calendar unit (D/H/M) into this number;
  the actual wall-clock length is determined by which timeframe's data the
  caller passes in, not by the factor name.
- ``tf_prefix`` (optional): only present when this factor is deliberately
  computed on a different timeframe than the strategy's base timeframe
  (e.g. a daily filter attached to an hourly strategy) — uses the same
  timeframe codes as ``librae`` (``h1``, ``d1``, ``m5``, ...).

Single-asset factors take only ``df`` (OHLCV shaped like
``data.ohlcv.get_ohlcv()``'s output). Cross-asset factors additionally need
``ref_close``, the reference asset's close series already asof-merged onto
``df``'s index (see ``data.cross_asset.attach_cross_asset_features``) —
callers pass it as a kwarg.
"""
from __future__ import annotations

from typing import Callable

import pandas as pd
import pandas_ta_classic as ta

from strategies.module.factors.operators import momentum, rolling_corr

FACTORS: dict[str, Callable[..., pd.Series]] = {
    # single-asset, pure OHLCV
    "d1_mom_p10": lambda df: momentum(df["close"], 10),
    "rsi_demeaned_p14": lambda df: ta.rsi(df["close"], 14) - 50.0,

    # cross-asset — require ref_close kwarg (see module docstring)
    "xasset_corr_p24": lambda df, ref_close: rolling_corr(
        df["close"].pct_change(), ref_close.pct_change(), 24
    ),
    "xasset_relmom_p24": lambda df, ref_close: momentum(df["close"], 24) - momentum(ref_close, 24),
}
