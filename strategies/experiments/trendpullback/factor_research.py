"""Retroactive factor validation for TrendPullback — covers both base
frequencies validated in report.md: H1 (deployed default) and M5 (the former
``trendpullback_m5`` family, merged into this folder via the
``gate_timeframe`` config knob — see ``utils.DEFAULT_PARAMS``). Filling the
gap this family was deployed with: no factor-significance step was ever run
before it went live.

One script covers both frequencies because the pipeline itself doesn't
change between them — only the profile (timeframe/gate/date range) does; see
``PROFILES`` below. M5 uses a shorter sample window than H1: bar density
means ~9 months already gives comparable event counts to H1's ~2.5 years,
kept tractable given ccxt's paginated public REST fetch with no DB cache
here.

Follows the pipeline documented in
strategies/RESEARCH_METHODOLOGY.md, mapped onto this repo's
actual tools: ``strategies.module.data.ohlcv.get_ohlcv`` (not ``utils/data.py``),
``librae.backtest.engine.Backtest`` (not ``BacktestService``) — no separate
hand-rolled simulator needed since this repo's real backtest engine is
directly runnable here. The MAE/MFE distribution and cross-asset robustness
checks only run for the H1 profile, matching report.md's scope — they were
never part of the M5 study.

No ``strategy.py`` exists for this family — factor validation didn't pass
(see report.md's conclusion: not recommended at either frequency), so per
the "factor validation must pass before a strategy is built" policy, the
originally-deployed ``TrendPullbackStrategy`` is kept here only as a local
backtest candidate, not exported or wired to any CLI/run_dispatch entrypoint.

Run: ``python -m strategies.experiments.trendpullback.factor_research``
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
from strategies.experiments.trendpullback.utils import (
    compute_entry_conditions,
    compute_exit_conditions,
    compute_features,
    prepare_signals,
)

# Decision asset / robustness asset (see "cross-asset robustness" below)
DECISION_ASSET = "BTCUSDT"
ROBUST_ASSET = "ETHUSDT"


class _TrendPullbackCandidate(BaseStrategy):
    """Candidate only — not exported, not a deployed strategy (see module
    docstring). Enter on pullback to EMA in uptrend, exit on trend break.

    Expects DataFrame to have pre-computed ``entry_signal``/``exit_signal``
    columns (see ``utils.prepare_signals``).
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

# Sample split + profile config, one entry per base frequency validated.
# Deployed holding horizon (24 bars) is not swept here — validating the
# factor at the horizon the strategy actually uses, not searching for a
# better one.
PROFILES: dict[str, dict] = {
    "H1": dict(
        timeframe="1h", gate_timeframe="1D", forward_periods=24,
        is_train_start="2024-01-01", is_train_end="2024-12-31",
        is_val_end="2025-08-31", oos_end="2026-07-01",
    ),
    "M5": dict(
        timeframe="5m", gate_timeframe="30min", forward_periods=24,
        is_train_start="2025-08-01", is_train_end="2025-12-31",
        is_val_end="2026-03-31", oos_end="2026-07-01",
    ),
}


def _fetch(symbol: str, timeframe: str, start: str, end: str) -> pd.DataFrame:
    return get_ohlcv(symbol, timeframe, data_source="binance_spot", start=start, end=end, warmup_periods=720)


def _prepare(base: pd.DataFrame, gate_timeframe: str, with_gate: bool) -> pd.DataFrame:
    """with_gate=True: deployed logic, via the shared ``prepare_signals()``
    (same function ``run.py``/``LiveTrader`` use — no separate research-only
    copy of the gate merge). with_gate=False: the required no-filter
    ablation baseline — same entry/exit conditions, ``daily_trend`` forced
    True."""
    if with_gate:
        return prepare_signals(base, {"gate_timeframe": gate_timeframe})
    out = compute_features(base)
    out["daily_trend"] = True
    out["entry_signal"] = compute_entry_conditions(out).values
    out["exit_signal"] = compute_exit_conditions(out).values
    return out


def _run_profile(name: str, profile: dict) -> None:
    print(f"\n########## Profile: {name} (base={profile['timeframe']}, gate={profile['gate_timeframe']}) ##########")
    print(f"Fetching {DECISION_ASSET} {profile['timeframe']} {profile['is_train_start']}..{profile['oos_end']}...")
    full = _fetch(DECISION_ASSET, profile["timeframe"], profile["is_train_start"], profile["oos_end"])
    full = full.set_index("timestamp")
    splits = split_is_val_oos(full, profile["is_train_end"], profile["is_val_end"], profile["oos_end"])

    print("\n=== Factor significance: event hit-rate, with-gate vs no-gate ===")
    rows = []
    for split_name, split_df in splits.items():
        for variant, with_gate in (("with_gate", True), ("no_gate", False)):
            df = _prepare(split_df, profile["gate_timeframe"], with_gate)
            row = test_event_hit_rate(df, DECISION_ASSET, f"{split_name}/{variant}", profile["forward_periods"])
            rows.append(row)
            print(f"  {row['label']:22s} n_events={row['n_events']:5d}  hit_rate={row['hit_rate']:.4f}  p_raw={row['p_raw']:.4f}")
    print_holm_corrected(rows)

    print("\n=== Backtest: zero-cost, IS-Train+IS-Val ===")
    fit_window = full.loc[profile["is_train_start"]:profile["is_val_end"]]
    for variant, with_gate in (("with_gate", True), ("no_gate", False)):
        df = _prepare(fit_window, profile["gate_timeframe"], with_gate)
        stats = run_engine_backtest(df, _TrendPullbackCandidate(max_hold_periods=24), DECISION_ASSET)
        print(f"  {variant:10s} {stats}")

    print("\n=== Backtest: OOS blind check (same params, never used to pick anything above) ===")
    for variant, with_gate in (("with_gate", True), ("no_gate", False)):
        df = _prepare(splits["OOS"], profile["gate_timeframe"], with_gate)
        stats = run_engine_backtest(df, _TrendPullbackCandidate(max_hold_periods=24), DECISION_ASSET)
        print(f"  {variant:10s} {stats}")

    if name != "H1":
        return

    print("\n=== MAE/MFE distribution: with-gate, IS-Train only ===")
    is_train_df = _prepare(splits["IS-Train"], profile["gate_timeframe"], True)
    stats = run_engine_backtest(
        is_train_df, _TrendPullbackCandidate(max_hold_periods=24), DECISION_ASSET, return_trades=True,
    )
    print(f"  {mae_mfe_percentiles(stats['trade_list'], is_train_df)}")

    print(f"\n=== Cross-asset robustness: {ROBUST_ASSET}, same params, no re-tuning ===")
    eth_full = _fetch(ROBUST_ASSET, profile["timeframe"], profile["is_train_start"], profile["oos_end"]).set_index("timestamp")
    for variant, with_gate in (("with_gate", True), ("no_gate", False)):
        df = _prepare(eth_full, profile["gate_timeframe"], with_gate)
        stats = run_engine_backtest(df, _TrendPullbackCandidate(max_hold_periods=24), ROBUST_ASSET)
        print(f"  {variant:10s} {stats}")


def main() -> None:
    for name, profile in PROFILES.items():
        _run_profile(name, profile)


if __name__ == "__main__":
    main()
