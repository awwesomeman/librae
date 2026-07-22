"""Factor validation for MTF Trend Momentum — re-validates, on this repo's own
data/engine, the two factors this family's originally-researched logic used:
``mom_1D_10`` (daily 10-bar momentum, used as a sign-gate) and ``mom_1H_12``
(hourly 12-bar momentum, used as a breakout-entry / momentum-loss-exit
trigger). The original research (a different project's utils, not runnable
in this repo) compared three candidates (trend+RSI reversion, trend+hourly
momentum breakout, pure-RSI-no-filter baseline) — the trend+RSI-reversion and
pure-RSI-no-filter candidates are already covered by
``strategies/experiments/mtf_trend_rsi`` (same ``mom_1D_10`` gate, RSI timing
factor). This script keeps this family's own distinguishing hypothesis: does
the hourly momentum breakout (gated by the same daily trend sign) do better
than the same breakout with no gate — following the identical with_gate/
no_gate ablation convention ``trendpullback``/``mtf_trend_rsi`` use, instead
of re-deriving the RSI candidates a second time here.

No ``strategy.py`` exists for this family — per the "factor validation must
pass before a strategy is built" policy, and this validation didn't pass
(see report.md's conclusion). The candidate Strategy used for backtest
checks below is intentionally local to this script, not a module-level
export.

Run: ``python -m strategies.experiments.mtf_trend_momentum.factor_research``
"""
from __future__ import annotations

import pandas as pd
import polars as pl
from factrix.metrics import directional_hit_rate
from factrix.preprocess import compute_forward_return

from librae.core.strategy import Action, BaseStrategy, Context
from strategies.module.data.ohlcv import get_ohlcv
from strategies.module.data.utils import resample_ohlcv
from strategies.module.factors.operators import momentum
from strategies.module.factors.utils import (
    print_holm_corrected,
    run_engine_backtest,
    test_event_hit_rate,
)
from strategies.module.utils import split_is_val_oos
from strategies.experiments.mtf_trend_momentum.utils import prepare_signals

DECISION_ASSET = "BTCUSDT"
ROBUST_ASSET = "ETHUSDT"

# Same H1 sample split as trendpullback/mtf_trend_rsi's factor_research.py —
# all three families validate on BTCUSDT H1 with a D1 gate, so using
# identical windows keeps the reports directly comparable.
IS_TRAIN_START, IS_TRAIN_END = "2024-01-01", "2024-12-31"
IS_VAL_END = "2025-08-31"
OOS_END = "2026-07-01"

# Event-hit-rate horizon for the deployed long/short entry signals (matches
# trendpullback/mtf_trend_rsi's convention: 24 H1 bars = 1 day).
ENTRY_FORWARD_PERIODS = 24
# Each momentum factor's own significance test uses a forward window equal
# to its own lookback (does the past-N-bar return predict the next-N-bar
# return) — a standard momentum-factor convention, not swept.
MOM_1D_FORWARD_DAYS = 10
MOM_1H_FORWARD_HOURS = 12


class _MTFTrendMomentumCandidate(BaseStrategy):
    """Candidate only — not exported, not a deployed strategy. Reads the
    long/short entry+exit booleans ``utils.prepare_signals`` computes."""

    def on_bar(self, ctx: Context) -> list[Action]:
        pos = ctx.positions.get(ctx.symbol)
        bar = ctx.bar

        if pos:
            if pos.side == "long" and bar.get("long_exit"):
                return [Action(type="close", symbol=ctx.symbol)]
            if pos.side == "short" and bar.get("short_exit"):
                return [Action(type="close", symbol=ctx.symbol)]
            return []

        if bar.get("long_entry"):
            return [Action(type="long", symbol=ctx.symbol)]
        if bar.get("short_entry"):
            return [Action(type="short", symbol=ctx.symbol)]
        return []


def _fetch(symbol: str, start: str, end: str) -> pd.DataFrame:
    return get_ohlcv(symbol, "1h", data_source="binance_spot", start=start, end=end, warmup_periods=720)


def _mom_1d_10_hit_rate(h1: pd.DataFrame, symbol: str, label: str) -> dict:
    """Continuous-factor significance: does mom_1D_10's sign predict the
    next MOM_1D_FORWARD_DAYS-day direction? (directional_hit_rate, not
    event_hit_rate — mom_1D_10 is a continuous factor, not a binary event.)"""
    d1 = resample_ohlcv(h1, "1D")
    mom = momentum(d1["close"], 10)
    n_valid = mom.notna().sum()
    if n_valid < 30:
        return {"label": label, "n_events": int(n_valid), "hit_rate": float("nan"), "p_raw": float("nan")}

    panel = pl.from_pandas(pd.DataFrame({
        "date": d1.index.tz_localize(None) if d1.index.tz is not None else d1.index,
        "asset_id": symbol,
        "factor": mom.values,
        "price": d1["close"].values,
    })).drop_nulls()
    panel = compute_forward_return(panel, forward_periods=MOM_1D_FORWARD_DAYS)
    result = directional_hit_rate(panel, factor_col="factor", return_col="forward_return")
    return {"label": label, "n_events": int(n_valid), "hit_rate": result.value, "p_raw": result.p_value}


def _mom_1h_12_hit_rate(h1: pd.DataFrame, symbol: str, label: str) -> dict:
    """Continuous-factor significance: does mom_1H_12's sign predict the
    next MOM_1H_FORWARD_HOURS-hour direction? Same convention as
    ``_mom_1d_10_hit_rate`` (own-lookback forward window), applied to the
    breakout timing factor instead of the daily gate."""
    mom = momentum(h1["close"], 12)
    n_valid = mom.notna().sum()
    if n_valid < 30:
        return {"label": label, "n_events": int(n_valid), "hit_rate": float("nan"), "p_raw": float("nan")}

    panel = pl.from_pandas(pd.DataFrame({
        "date": h1.index.tz_localize(None) if h1.index.tz is not None else h1.index,
        "asset_id": symbol,
        "factor": mom.values,
        "price": h1["close"].values,
    })).drop_nulls()
    panel = compute_forward_return(panel, forward_periods=MOM_1H_FORWARD_HOURS)
    result = directional_hit_rate(panel, factor_col="factor", return_col="forward_return")
    return {"label": label, "n_events": int(n_valid), "hit_rate": result.value, "p_raw": result.p_value}


def _short_hit_rate(h1: pd.DataFrame, symbol: str, label: str, forward_periods: int) -> dict:
    """short_entry's success means price falls — invert the price series
    before feeding test_event_hit_rate() so its "positive forward return"
    check ends up testing "did price actually decline", reusing the shared
    helper as-is instead of writing a short-specific variant."""
    inverted = h1.copy()
    inverted["close"] = -inverted["close"]
    return test_event_hit_rate(inverted, symbol, label, forward_periods, signal_col="short_entry")


def _run_backtests(full: pd.DataFrame, splits: dict, symbol: str) -> None:
    print("\n=== Backtest: zero-cost, IS-Train+IS-Val ===")
    fit_window = full.loc[IS_TRAIN_START:IS_VAL_END]
    for variant, use_gate in (("with_gate", True), ("no_gate", False)):
        df = prepare_signals(fit_window, {"use_gate": use_gate})
        stats = run_engine_backtest(df, _MTFTrendMomentumCandidate(), symbol)
        print(f"  {variant:10s} {stats}")

    print("\n=== Backtest: OOS blind check ===")
    for variant, use_gate in (("with_gate", True), ("no_gate", False)):
        df = prepare_signals(splits["OOS"], {"use_gate": use_gate})
        stats = run_engine_backtest(df, _MTFTrendMomentumCandidate(), symbol)
        print(f"  {variant:10s} {stats}")


def main() -> None:
    print(f"Fetching {DECISION_ASSET} H1 {IS_TRAIN_START}..{OOS_END}...")
    full = _fetch(DECISION_ASSET, IS_TRAIN_START, OOS_END).set_index("timestamp")
    splits = split_is_val_oos(full, IS_TRAIN_END, IS_VAL_END, OOS_END)

    print("\n=== Factor significance: mom_1D_10 (continuous, directional hit-rate) ===")
    mom_1d_rows = []
    for split_name, split_df in splits.items():
        row = _mom_1d_10_hit_rate(split_df, DECISION_ASSET, split_name)
        mom_1d_rows.append(row)
        print(f"  {row['label']:10s} n={row['n_events']:4d}  hit_rate={row['hit_rate']:.4f}  p_raw={row['p_raw']:.4f}")
    print_holm_corrected(mom_1d_rows)

    print("\n=== Factor significance: mom_1H_12 (continuous, directional hit-rate) ===")
    mom_1h_rows = []
    for split_name, split_df in splits.items():
        row = _mom_1h_12_hit_rate(split_df, DECISION_ASSET, split_name)
        mom_1h_rows.append(row)
        print(f"  {row['label']:10s} n={row['n_events']:4d}  hit_rate={row['hit_rate']:.4f}  p_raw={row['p_raw']:.4f}")
    print_holm_corrected(mom_1h_rows)

    print("\n=== Factor significance: deployed long/short entry signals (event hit-rate) ===")
    entry_rows = []
    for split_name, split_df in splits.items():
        for variant, use_gate in (("with_gate", True), ("no_gate", False)):
            df = prepare_signals(split_df, {"use_gate": use_gate})
            long_row = test_event_hit_rate(df, DECISION_ASSET, f"{split_name}/{variant}/long", ENTRY_FORWARD_PERIODS, signal_col="long_entry")
            short_row = _short_hit_rate(df, DECISION_ASSET, f"{split_name}/{variant}/short", ENTRY_FORWARD_PERIODS)
            entry_rows += [long_row, short_row]
            for row in (long_row, short_row):
                print(f"  {row['label']:24s} n_events={row['n_events']:5d}  hit_rate={row['hit_rate']:.4f}  p_raw={row['p_raw']:.4f}")
    print_holm_corrected(entry_rows)

    _run_backtests(full, splits, DECISION_ASSET)

    print(f"\n=== Cross-asset robustness: {ROBUST_ASSET}, same params, no re-tuning ===")
    eth_full = _fetch(ROBUST_ASSET, IS_TRAIN_START, OOS_END).set_index("timestamp")
    for variant, use_gate in (("with_gate", True), ("no_gate", False)):
        df = prepare_signals(eth_full, {"use_gate": use_gate})
        stats = run_engine_backtest(df, _MTFTrendMomentumCandidate(), ROBUST_ASSET)
        print(f"  {variant:10s} {stats}")


if __name__ == "__main__":
    main()
