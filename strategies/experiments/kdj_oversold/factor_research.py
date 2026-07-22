"""Retroactive factor validation for KDJ Oversold — this family's ``run.py``/
``utils.py`` are a legitimate, different pattern (level-based signal dumped
to DB via ``save_signal_results`` for ad-hoc analysis, not a position-managed
backtest — see both files' docstrings) but, unlike the DB path, had never
had a proper factor-significance step run before anyone builds on it. This
fills that gap, following strategies/RESEARCH_METHODOLOGY.md's pipeline,
same shape as strategies/experiments/trendpullback/factor_research.py and
strategies/experiments/mtf_trend_rsi/factor_research.py.

``entry_signal`` (J < j_threshold) has no separate "gate" to strip out —
it *is* the whole filter — so the required no-filter baseline here is
"always try to enter" (ablation: entry_signal forced True every bar,
same exit rule) instead of a with/without-gate split.

No ``strategy.py`` exists for this family — factor validation is what this
script performs; see report.md's conclusion for whether it passed. The
candidate Strategy used for backtest checks is local to this script, not a
module-level export, per the "factor validation must pass before a
strategy is built" policy.

Run: ``python -m strategies.experiments.kdj_oversold.factor_research``
"""
from __future__ import annotations

import pandas as pd

from librae.core.strategy import Action, BaseStrategy, Context
from strategies.module.data.ohlcv import get_ohlcv
from strategies.module.factors.utils import (
    mae_mfe_percentiles,
    print_holm_corrected,
    run_engine_backtest,
    test_event_hit_rate,
)
from strategies.module.utils import split_is_val_oos
from strategies.experiments.kdj_oversold.utils import compute_exit_signal, prepare_signals

DECISION_ASSET = "BTCUSDT"
ROBUST_ASSET = "ETHUSDT"

# run.py's default START ("2025-10-01") is only ~9 months of H1 data — event
# hit-rate is already high-n there (~1,600 J<20 bars), but IS/Val/OOS needs
# three separate windows, each with its own event count. Extended back to
# match strategies/experiments/trendpullback's H1 window (~2.5 years) so all
# three slices have comparable statistical power to this repo's other
# validated families, and so this report is directly comparable to theirs.
IS_TRAIN_START, IS_TRAIN_END = "2024-01-01", "2024-12-31"
IS_VAL_END = "2025-08-31"
OOS_END = "2026-07-01"

# Deployed exit horizon (max_hold_periods, see utils.DEFAULT_PARAMS) doubles
# as the event-significance forward window — validating the factor at the
# horizon the candidate actually trades, not searching for a better one.
FORWARD_PERIODS = 24


class _KDJOversoldCandidate(BaseStrategy):
    """Candidate only — not exported, not a deployed strategy (see module
    docstring). Mean-reversion long: enter on entry_signal, exit when J
    recovers above exit_j_threshold or max_hold_periods elapses.

    Expects DataFrame to have pre-computed entry_signal/exit_signal columns
    (see ``_prepare`` below).
    """

    def __init__(self, max_hold_periods: int = 24) -> None:
        self.max_hold_periods = max_hold_periods

    def on_bar(self, ctx: Context) -> list[Action]:
        pos = ctx.positions.get(ctx.symbol)

        if pos:
            if ctx.bar.get("exit_signal") or pos.periods_held >= self.max_hold_periods:
                return [Action(type="close", symbol=ctx.symbol)]
            return []

        if ctx.bar.get("entry_signal"):
            return [Action(type="long", symbol=ctx.symbol)]

        return []


def _fetch(symbol: str, start: str, end: str) -> pd.DataFrame:
    return get_ohlcv(symbol, "1h", data_source="binance_spot", start=start, end=end, warmup_periods=200)


def _prepare(base: pd.DataFrame, use_signal: bool) -> pd.DataFrame:
    """use_signal=True: deployed logic (entry on J < j_threshold), via the
    shared ``prepare_signals()`` (same function run.py uses — no separate
    research-only copy). use_signal=False: the required no-filter
    ablation baseline — entry_signal forced True every bar (KDJ isn't
    consulted at all for entry), same exit rule."""
    out = prepare_signals(base)
    if not use_signal:
        out["entry_signal"] = 1
    out["exit_signal"] = compute_exit_signal(out)
    return out


def _run_backtests(full: pd.DataFrame, splits: dict, symbol: str) -> None:
    print("\n=== Backtest: zero-cost, IS-Train+IS-Val ===")
    fit_window = full.loc[IS_TRAIN_START:IS_VAL_END]
    for variant, use_signal in (("kdj_signal", True), ("always_enter", False)):
        df = _prepare(fit_window, use_signal)
        stats = run_engine_backtest(df, _KDJOversoldCandidate(max_hold_periods=FORWARD_PERIODS), symbol)
        print(f"  {variant:12s} {stats}")

    print("\n=== Backtest: OOS blind check (same params, never used to pick anything above) ===")
    for variant, use_signal in (("kdj_signal", True), ("always_enter", False)):
        df = _prepare(splits["OOS"], use_signal)
        stats = run_engine_backtest(df, _KDJOversoldCandidate(max_hold_periods=FORWARD_PERIODS), symbol)
        print(f"  {variant:12s} {stats}")


def main() -> None:
    print(f"Fetching {DECISION_ASSET} H1 {IS_TRAIN_START}..{OOS_END}...")
    full = _fetch(DECISION_ASSET, IS_TRAIN_START, OOS_END).set_index("timestamp")
    full.index.name = "ts"
    splits = split_is_val_oos(full, IS_TRAIN_END, IS_VAL_END, OOS_END)

    print("\n=== Factor significance: entry_signal event hit-rate (J < j_threshold) ===")
    rows = []
    for split_name, split_df in splits.items():
        df = prepare_signals(split_df)
        row = test_event_hit_rate(df, DECISION_ASSET, split_name, FORWARD_PERIODS)
        rows.append(row)
        print(f"  {row['label']:10s} n_events={row['n_events']:5d}  hit_rate={row['hit_rate']:.4f}  p_raw={row['p_raw']:.4f}")
    print_holm_corrected(rows)

    _run_backtests(full, splits, DECISION_ASSET)

    print("\n=== MAE/MFE distribution: kdj_signal, IS-Train only ===")
    is_train_df = _prepare(splits["IS-Train"], True)
    stats = run_engine_backtest(
        is_train_df, _KDJOversoldCandidate(max_hold_periods=FORWARD_PERIODS), DECISION_ASSET, return_trades=True,
    )
    print(f"  {mae_mfe_percentiles(stats['trade_list'], is_train_df)}")

    print(f"\n=== Cross-asset robustness: {ROBUST_ASSET}, same params, no re-tuning ===")
    eth_full = _fetch(ROBUST_ASSET, IS_TRAIN_START, OOS_END).set_index("timestamp")
    eth_full.index.name = "ts"
    for variant, use_signal in (("kdj_signal", True), ("always_enter", False)):
        df = _prepare(eth_full, use_signal)
        stats = run_engine_backtest(df, _KDJOversoldCandidate(max_hold_periods=FORWARD_PERIODS), ROBUST_ASSET)
        print(f"  {variant:12s} {stats}")


if __name__ == "__main__":
    main()
