"""Factor validation for MTF 4H Regime-Switching Reversal + Funding —
re-validates, on this repo's own data/engine, the daily |mom_1D_10| regime
gate that switches between a 4H range-mode reversal sub-strategy
(roc_3/rsi_6 extremes) and a trend-mode funding-momentum sub-strategy
(funding-rate crowding confirming the daily trend). The original research (a
different project's utils, not runnable in this repo) claimed a headline
`vwap_dist_12` factor (daily rolling-VWAP distance, "PASS" per its own
oos_decay table) and a regime-switching composite Sharpe of 4.9682 in
IS-Val — but `vwap_dist_12` is never actually computed or used anywhere in
that project's runnable script, only mentioned in its prose tables, and its
own report.md (see strategies/FACTOR_ANALYSIS.md's index entry) was never
independently reviewed. This script tests `vwap_dist_12` for real (it never
was, before this) and independently re-derives whether regime-switching adds
anything over its two constituent sub-strategies, using real OHLCV, real
funding-rate history, and the real engine.

No `strategy.py` exists for this family yet — per the "factor validation
must pass before a strategy is built" policy; the candidate Strategy below
is intentionally local to this script.

Run: ``python -m strategies.experiments.mtf_4h_regime_reversal_funding.factor_research``
"""
from __future__ import annotations

import pandas as pd
import polars as pl
from factrix.metrics import directional_hit_rate
from factrix.preprocess import compute_forward_return

from librae.core.strategy import Action, BaseStrategy, Context
from strategies.module.data.funding import attach_funding_features
from strategies.module.data.ohlcv import get_ohlcv
from strategies.module.data.utils import resample_ohlcv
from strategies.module.factors.utils import (
    mae_mfe_percentiles,
    print_holm_corrected,
    run_engine_backtest,
    test_event_hit_rate,
)
from strategies.module.utils import split_is_val_oos
from strategies.experiments.mtf_4h_regime_reversal_funding.utils import (
    compute_daily_features,
    prepare_signals,
)

DECISION_ASSET = "BTCUSDT"
ROBUST_ASSET = "ETHUSDT"
PERP_SYMBOL = {"BTCUSDT": "BTC/USDT:USDT", "ETHUSDT": "ETH/USDT:USDT"}

# Sample split on 4H bars. Funding-rate history (fetch_funding_rate_history,
# binanceusdm) is available cleanly from 2024-01-01 through the present with
# no gaps (verified via a direct fetch before picking these dates) — the
# binding constraint here, not 4H bar density (4H gives ~6 bars/day, plenty
# for these windows). Kept identical to trendpullback/mtf_trend_rsi's H1
# windows so all three reports read on the same calendar, even though the
# base timeframe differs.
IS_TRAIN_START, IS_TRAIN_END = "2024-01-01", "2024-12-31"
IS_VAL_END = "2025-08-31"
OOS_END = "2026-07-01"

MOM_FORWARD_DAYS = 10   # matches mom_1D_10's own lookback, standard momentum-factor convention
VWAP_FORWARD_DAYS = 12  # matches vwap_dist_12's own window
FUNDING_FORWARD_BARS = 16  # matches trend-mode's own max_hold (4H bars)
RANGE_FORWARD_BARS = 12    # matches range-mode's own max_hold (4H bars)
TREND_FORWARD_BARS = 16    # matches trend-mode's own max_hold (4H bars)


class _RegimeReversalFundingCandidate(BaseStrategy):
    """Candidate only — not exported, not a deployed strategy. Reads the
    long/short entry+exit booleans and regime-dependent ``max_hold`` that
    ``utils.prepare_signals`` computes."""

    def on_bar(self, ctx: Context) -> list[Action]:
        pos = ctx.positions.get(ctx.symbol)
        bar = ctx.bar

        if pos:
            exit_signal = bar.get("long_exit") if pos.side == "long" else bar.get("short_exit")
            if exit_signal or pos.periods_held >= bar.get("max_hold", 0):
                return [Action(type="close", symbol=ctx.symbol)]
            return []

        if bar.get("long_entry"):
            return [Action(type="long", symbol=ctx.symbol)]
        if bar.get("short_entry"):
            return [Action(type="short", symbol=ctx.symbol)]
        return []


def _fetch(symbol: str, start: str, end: str) -> pd.DataFrame:
    df = get_ohlcv(symbol, "4h", data_source="binance_spot", start=start, end=end, warmup_periods=200)
    df = attach_funding_features(df, PERP_SYMBOL[symbol], start, end)
    return df.set_index("timestamp")


def _continuous_hit_rate(series: pd.Series, price: pd.Series, symbol: str, label: str, forward_periods: int) -> dict:
    """Directional hit-rate for a continuous factor (not a binary event) —
    same pattern as mtf_trend_rsi/factor_research.py's `_mom_1d_10_hit_rate`."""
    n_valid = series.notna().sum()
    if n_valid < 30:
        return {"label": label, "n_events": int(n_valid), "hit_rate": float("nan"), "p_raw": float("nan")}
    idx = series.index.tz_localize(None) if series.index.tz is not None else series.index
    panel = pl.from_pandas(pd.DataFrame({
        "date": idx, "asset_id": symbol, "factor": series.values, "price": price.values,
    })).drop_nulls()
    panel = compute_forward_return(panel, forward_periods=forward_periods)
    result = directional_hit_rate(panel, factor_col="factor", return_col="forward_return")
    return {"label": label, "n_events": int(n_valid), "hit_rate": result.value, "p_raw": result.p_value}


def _short_hit_rate(df: pd.DataFrame, symbol: str, label: str, forward_periods: int, signal_col: str) -> dict:
    """short_entry's success means price falls — invert close before feeding
    test_event_hit_rate() (same trick as mtf_trend_rsi/factor_research.py)."""
    inverted = df.copy()
    inverted["close"] = -inverted["close"]
    return test_event_hit_rate(inverted, symbol, label, forward_periods, signal_col=signal_col)


def _run_backtests(full: pd.DataFrame, splits: dict, symbol: str) -> None:
    variants = [
        ("switching", None),
        ("range_only", "ranging"),
        ("trend_only", "trending"),
    ]
    print("\n=== Backtest: zero-cost, IS-Train+IS-Val ===")
    fit_window = full.loc[IS_TRAIN_START:IS_VAL_END]
    for name, force_regime in variants:
        df = prepare_signals(fit_window, {"force_regime": force_regime})
        stats = run_engine_backtest(df, _RegimeReversalFundingCandidate(), symbol)
        print(f"  {name:12s} {stats}")

    print("\n=== Backtest: OOS blind check ===")
    for name, force_regime in variants:
        df = prepare_signals(splits["OOS"], {"force_regime": force_regime})
        stats = run_engine_backtest(df, _RegimeReversalFundingCandidate(), symbol)
        print(f"  {name:12s} {stats}")


def main() -> None:
    print(f"Fetching {DECISION_ASSET} 4H + funding {IS_TRAIN_START}..{OOS_END}...")
    full = _fetch(DECISION_ASSET, IS_TRAIN_START, OOS_END)
    splits = split_is_val_oos(full, IS_TRAIN_END, IS_VAL_END, OOS_END)

    print("\n=== Factor significance: mom_1D_10 (continuous, directional hit-rate) ===")
    mom_rows = []
    for split_name, split_df in splits.items():
        d1 = resample_ohlcv(split_df, "1D")
        feats = compute_daily_features(d1)
        row = _continuous_hit_rate(feats["mom_1D_10"], feats["close"], DECISION_ASSET, split_name, MOM_FORWARD_DAYS)
        mom_rows.append(row)
        print(f"  {row['label']:10s} n={row['n_events']:4d}  hit_rate={row['hit_rate']:.4f}  p_raw={row['p_raw']:.4f}")
    print_holm_corrected(mom_rows)

    print("\n=== Factor significance: vwap_dist_12 (continuous, directional hit-rate) ===")
    print("(the original report's headline 'PASS' claim — never actually computed in its runnable script; tested here for the first time)")
    vwap_rows = []
    for split_name, split_df in splits.items():
        d1 = resample_ohlcv(split_df, "1D")
        feats = compute_daily_features(d1)
        row = _continuous_hit_rate(feats["vwap_dist_12"], feats["close"], DECISION_ASSET, split_name, VWAP_FORWARD_DAYS)
        vwap_rows.append(row)
        print(f"  {row['label']:10s} n={row['n_events']:4d}  hit_rate={row['hit_rate']:.4f}  p_raw={row['p_raw']:.4f}")
    print_holm_corrected(vwap_rows)

    print("\n=== Factor significance: funding_z_3d (continuous, directional hit-rate, 4H) ===")
    funding_rows = []
    for split_name, split_df in splits.items():
        row = _continuous_hit_rate(split_df["funding_z_3d"], split_df["close"], DECISION_ASSET, split_name, FUNDING_FORWARD_BARS)
        funding_rows.append(row)
        print(f"  {row['label']:10s} n={row['n_events']:4d}  hit_rate={row['hit_rate']:.4f}  p_raw={row['p_raw']:.4f}")
    print_holm_corrected(funding_rows)

    print("\n=== Factor significance: deployed regime-switching entry signals (event hit-rate) ===")
    entry_rows = []
    for split_name, split_df in splits.items():
        df = prepare_signals(split_df, {"force_regime": None})
        df["range_long_entry"] = df["is_ranging"] & df["long_entry"]
        df["range_short_entry"] = df["is_ranging"] & df["short_entry"]
        df["trend_long_entry"] = (~df["is_ranging"]) & df["long_entry"]
        df["trend_short_entry"] = (~df["is_ranging"]) & df["short_entry"]

        rl = test_event_hit_rate(df, DECISION_ASSET, f"{split_name}/range/long", RANGE_FORWARD_BARS, signal_col="range_long_entry")
        rs = _short_hit_rate(df, DECISION_ASSET, f"{split_name}/range/short", RANGE_FORWARD_BARS, "range_short_entry")
        tl = test_event_hit_rate(df, DECISION_ASSET, f"{split_name}/trend/long", TREND_FORWARD_BARS, signal_col="trend_long_entry")
        ts = _short_hit_rate(df, DECISION_ASSET, f"{split_name}/trend/short", TREND_FORWARD_BARS, "trend_short_entry")
        entry_rows += [rl, rs, tl, ts]
        for row in (rl, rs, tl, ts):
            print(f"  {row['label']:24s} n_events={row['n_events']:5d}  hit_rate={row['hit_rate']:.4f}  p_raw={row['p_raw']:.4f}")
    print_holm_corrected(entry_rows)

    _run_backtests(full, splits, DECISION_ASSET)

    print("\n=== MAE/MFE distribution: switching composite, IS-Train only ===")
    is_train_df = prepare_signals(splits["IS-Train"], {"force_regime": None})
    stats = run_engine_backtest(
        is_train_df, _RegimeReversalFundingCandidate(), DECISION_ASSET, return_trades=True,
    )
    print(f"  {mae_mfe_percentiles(stats['trade_list'], is_train_df)}")

    print(f"\n=== Cross-asset robustness: {ROBUST_ASSET}, same params, no re-tuning ===")
    eth_full = _fetch(ROBUST_ASSET, IS_TRAIN_START, OOS_END)
    for name, force_regime in (("switching", None), ("range_only", "ranging"), ("trend_only", "trending")):
        df = prepare_signals(eth_full, {"force_regime": force_regime})
        stats = run_engine_backtest(df, _RegimeReversalFundingCandidate(), ROBUST_ASSET)
        print(f"  {name:12s} {stats}")


if __name__ == "__main__":
    main()
