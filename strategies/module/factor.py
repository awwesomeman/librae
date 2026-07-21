"""Factor-analysis tooling — factrix event-panel construction, Holm-correction
reporting. Extracted from ``strategies/trendpullback/factor_research.py``
(①~⑧ pipeline per ``strategies/experiments/RESEARCH_METHODOLOGY.md``) so the
next ``factor_research.py`` doesn't retype the same boilerplate.

Deliberately does NOT hold strategy-specific signal computation (EMA/RSI/
momentum) — that stays in each strategy's own ``utils.py``. This module is
only for factor-significance-testing plumbing; generic (non-factor) sample
splitting lives in ``strategies/module/utils.py`` instead.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import polars as pl


def build_event_panel(h1: pd.DataFrame, symbol: str, signal_col: str = "entry_signal") -> pl.DataFrame:
    """Long-format panel factrix needs: (date, asset_id, factor, price).
    `factor` = 1 at `signal_col` events, 0 elsewhere — the event-density
    shape ``factrix.metrics.event_quality.event_hit_rate`` expects."""
    out = pd.DataFrame({
        "date": h1.index.tz_localize(None) if h1.index.tz is not None else h1.index,
        "asset_id": symbol,
        "factor": h1[signal_col].astype(int).values,
        "price": h1["close"].values,
    })
    return pl.from_pandas(out)


def print_holm_corrected(rows: list[dict], alpha: float = 0.05) -> None:
    """Print raw -> Holm-corrected p-values for a list of
    ``{"label": str, "p_raw": float}`` dicts (NaN entries skipped) — the
    FWER-correction reporting step every factor-significance grid needs
    (RESEARCH_METHODOLOGY.md ③'s "先判斷這個決策要的是 FWER 還是 FDR")."""
    from factrix.stats import holm_adjusted_p

    p_values = [r["p_raw"] for r in rows if not np.isnan(r["p_raw"])]
    if not p_values:
        return
    p_holm = holm_adjusted_p(p_values)
    print(f"\n  Holm-corrected (FWER, n={len(p_values)} tests, alpha={alpha}):")
    j = 0
    for r in rows:
        if not np.isnan(r["p_raw"]):
            verdict = "PASS" if p_holm[j] < alpha else "fail"
            print(f"    {r['label']:22s} p_holm={p_holm[j]:.4f}  {verdict}")
            j += 1
