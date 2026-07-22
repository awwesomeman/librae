"""Factor-analysis tooling — factrix event-panel construction, Holm-correction
reporting, real-engine backtest summary, MAE/MFE distribution. Extracted from
``strategies/experiments/trendpullback/factor_research.py`` (following the pipeline in
``strategies/RESEARCH_METHODOLOGY.md``) so the next
``factor_research.py`` doesn't retype the same boilerplate.

Deliberately does NOT hold strategy-specific signal computation (EMA/RSI/
momentum) — that stays in each strategy's own ``utils.py``. This module is
only for factor-significance-testing plumbing; generic (non-factor) sample
splitting lives in ``strategies/module/utils.py`` instead.
"""
from __future__ import annotations

from typing import Any

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
    (see RESEARCH_METHODOLOGY.md's "先判斷這個決策要的是 FWER 還是 FDR")."""
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


def test_event_hit_rate(
    df: pd.DataFrame,
    symbol: str,
    label: str,
    forward_periods: int,
    *,
    signal_col: str = "entry_signal",
    min_events: int = 8,
) -> dict:
    """Factor significance via factrix's event_hit_rate (Pesaran-Timmermann
    binomial test, H0: hit rate = 0.5). Returns a row dict shaped for
    ``print_holm_corrected()``. Below ``min_events`` the test is skipped
    (NaN hit_rate/p_raw) — the binomial test isn't reliable on too few
    events."""
    from factrix.metrics.event_quality import event_hit_rate
    from factrix.preprocess import compute_forward_return

    n_events = int(df[signal_col].sum())
    if n_events < min_events:
        return {"label": label, "n_events": n_events, "hit_rate": float("nan"), "p_raw": float("nan")}

    panel = build_event_panel(df, symbol, signal_col=signal_col)
    panel = compute_forward_return(panel, forward_periods=forward_periods)
    result = event_hit_rate(panel, factor_col="factor", return_col="forward_return")
    return {"label": label, "n_events": n_events, "hit_rate": result.value, "p_raw": result.p_value}


def run_engine_backtest(
    df: pd.DataFrame,
    strategy: Any,
    symbol: str,
    *,
    initial_balance: float = 100_000,
    cost_model: Any | None = None,
    return_trades: bool = False,
) -> dict:
    """Real-engine backtest (``librae.backtest.engine.Backtest``) reduced to
    the 4-metric summary every ``strategies/*/report.md`` uses: total_return,
    sharpe, max_drawdown, trades. ``df`` needs a plain DatetimeIndex plus
    whatever signal columns ``strategy`` reads — this wraps the MultiIndex
    plumbing ``Backtest`` needs so callers don't repeat it per script.
    ``strategy`` is a ready-to-run instance (its params are the caller's
    concern, not this helper's). Pass ``return_trades=True`` to also get the
    raw trade list under ``"trade_list"`` (e.g. for ``mae_mfe_percentiles``)."""
    from librae.backtest.engine import Backtest
    from librae.core.cost_model import CostModel

    work = df.copy()
    work.index.name = "ts"
    mi = pd.MultiIndex.from_arrays([[symbol] * len(work), work.index], names=["symbol", "datetime"])
    work = work.set_index(mi)

    bt = Backtest(
        work, strategy, initial_balance=initial_balance,
        cost_model=cost_model or CostModel.zero(), data_source="research",
    )
    result = bt.run()
    bt.build_output(annualize=True)
    m = bt.metrics
    out = {
        "trades": m.trades,
        "sharpe": m.sharpe,
        "max_drawdown": m.max_drawdown,
        "total_return": m.total_return,
    }
    if return_trades:
        out["trade_list"] = result.trades
    return out


def mae_mfe_percentiles(trades: list, df: pd.DataFrame) -> dict:
    """MAE/MFE distribution across trades (long-only): max adverse/favorable
    excursion between entry_at/exit_at, as % of entry_price. Matches the
    SL/TP-calibration convention in ``strategies/experiments/*/report.md``:
    SL candidate = P75(|MAE|), TP candidate = P50(MFE)."""
    if not trades:
        return {"n": 0}
    mfes, maes = [], []
    for t in trades:
        window = df.loc[t.entry_at:t.exit_at]
        if window.empty:
            continue
        mfes.append((window["high"].max() - t.entry_price) / t.entry_price * 100)
        maes.append((window["low"].min() - t.entry_price) / t.entry_price * 100)
    mfes, maes = np.array(mfes), np.array(maes)
    return {
        "n": len(mfes),
        "median_mae": float(np.median(maes)),
        "median_mfe": float(np.median(mfes)),
        "p75_abs_mae": float(np.percentile(np.abs(maes), 75)),
        "p50_mfe": float(np.median(mfes)),
    }
