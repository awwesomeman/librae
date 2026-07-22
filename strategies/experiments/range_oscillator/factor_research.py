"""Factor validation for Range Oscillator — Keltner-channel mean-reversion
core factor (Bollinger %b, demeaned) plus a Trend+Vol+Amp+OI combined entry
filter, re-validated on this repo's real data/engine.

Ported from ``range_oscillator_research.py`` (a different project's
``utils/`` package — ``utils.data``/``utils.cached_kline``/``utils.universe``/
``utils.factors``/``utils.stats``/``utils.engine_check``/``utils.mfe_mae``/
``utils.backtest_sim``/``utils.open_interest`` — none of which exist in this
repo, so that script never ran here). The old script's own biggest
methodology flaw is fixed here too: it picked the winning filter candidate by
comparing all four directly on OOS, i.e. the only blind-test window was used
to make the selection decision. This version selects on IS-Val only and
treats OOS as a genuine blind check (see report.md).

No ``strategy.py`` is created unless factor validation clearly passes (see
RESEARCH_METHODOLOGY.md's policy) — see report.md's conclusion for whether it
does. The candidate Strategy used for backtest checks below is intentionally
local to this script, not a module-level export.

Run: ``python -m strategies.experiments.range_oscillator.factor_research``
"""
from __future__ import annotations

import pandas as pd
import polars as pl
from factrix.metrics import directional_hit_rate, oos_decay
from factrix.preprocess import compute_forward_return
from factrix.stats import holm_adjusted_p

import factrix as fx
from librae.core.strategy import Action, BaseStrategy, Context
from strategies.module.data.ohlcv import get_ohlcv
from strategies.module.factors.utils import mae_mfe_percentiles, run_engine_backtest
from strategies.module.utils import split_is_val_oos
from strategies.experiments.range_oscillator.utils import (
    attach_oi_regime,
    compute_features,
    compute_signal_conditions,
    merge_daily_trend,
)

DECISION_ASSET = "BTCUSDT"
ROBUST_ASSET = "ETHUSDT"

# Same H1 sample split as trendpullback/mtf_trend_rsi's factor_research.py —
# all three families validate on BTCUSDT H1 with a D1 gate, so identical
# windows keep the reports directly comparable.
IS_TRAIN_START, IS_TRAIN_END = "2024-01-01", "2024-12-31"
IS_VAL_END = "2025-08-31"
OOS_END = "2026-07-01"

HORIZONS = [1, 4, 12, 24]
ALPHA = 0.05

CANDIDATES = [
    {"name": "No filter (baseline)", "use_vol_amp_filter": False, "use_trend_filter": False, "needs_oi": False, "consolidating_col": "is_consolidating"},
    {"name": "Vol+Amp filter only", "use_vol_amp_filter": True, "use_trend_filter": False, "needs_oi": False, "consolidating_col": "is_consolidating"},
    {"name": "Trend+Vol+Amp (deployed)", "use_vol_amp_filter": True, "use_trend_filter": True, "needs_oi": False, "consolidating_col": "is_consolidating"},
    {"name": "Trend+Vol+Amp+OI (combined)", "use_vol_amp_filter": True, "use_trend_filter": True, "needs_oi": True, "consolidating_col": "is_consolidating_oi"},
]


class _RangeOscillatorCandidate(BaseStrategy):
    """Candidate only — not exported, not a deployed strategy. Reads the
    long/short entry+exit booleans ``utils.compute_signal_conditions``
    computes."""

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


def _bb_panel(df: pd.DataFrame, symbol: str) -> pl.DataFrame:
    idx = df.index.tz_localize(None) if df.index.tz is not None else df.index
    out = pd.DataFrame({"date": idx, "asset_id": symbol, "bb_pct_b": df["bb_pct_b"].values, "price": df["close"].values})
    return pl.from_pandas(out).drop_nulls()


def _candidate_frame(cand: dict, base_frame: pd.DataFrame, oi_frame: pd.DataFrame, params_extra: dict | None = None) -> pd.DataFrame:
    frame = oi_frame if cand["needs_oi"] else base_frame
    params = {
        "use_vol_amp_filter": cand["use_vol_amp_filter"],
        "use_trend_filter": cand["use_trend_filter"],
        "consolidating_col": cand["consolidating_col"],
        **(params_extra or {}),
    }
    return compute_signal_conditions(frame, params)


def main() -> None:
    print(f"Fetching {DECISION_ASSET} H1 {IS_TRAIN_START}..{OOS_END}...")
    raw = _fetch(DECISION_ASSET, IS_TRAIN_START, OOS_END).set_index("timestamp")
    base = compute_features(raw)
    base = merge_daily_trend(base, raw)
    print(f"OI coverage fetch {IS_TRAIN_START}..{OOS_END}...")
    oi_full = attach_oi_regime(base, DECISION_ASSET, IS_TRAIN_START, OOS_END)
    print(f"OI-consolidating share of full BTC window: {oi_full['oi_consolidating'].mean()*100:.1f}% ({len(oi_full)}/{len(base)} rows with OI coverage)")

    splits = split_is_val_oos(base, IS_TRAIN_END, IS_VAL_END, OOS_END)
    splits_oi = split_is_val_oos(oi_full, IS_TRAIN_END, IS_VAL_END, OOS_END)

    print("\n=== Factor significance: bb_pct_b (continuous, directional hit-rate, IS-Train, multi-horizon) ===")
    pl_train = _bb_panel(splits["IS-Train"], DECISION_ASSET)
    sweep = fx.evaluate_horizons(pl_train, metrics={"dir_hit": directional_hit_rate()}, factor_cols=["bb_pct_b"], forward_periods=HORIZONS)
    horizon_rows = []
    print(f"{'h':>3} | {'hit_rate':>8} | {'p_raw':>8}")
    for r in sweep:
        m = r.metrics["dir_hit"]
        horizon_rows.append((r.forward_periods, m.value, m.p_value))
        print(f"{r.forward_periods:>3} | {m.value:>8.4f} | {m.p_value:>8.4f}")

    # Two-sided transform BEFORE Holm correction: this factor is tested for
    # both a trend effect (p near 0) and a reversal effect (p near 1, the
    # strategy's actual mean-reversion assumption) — Holm needs a
    # small-p-is-significant statistic. FWER (not FDR) because this is a
    # grid search picking a SINGLE winning horizon (see RESEARCH_METHODOLOGY.md
    # 's FWER/FDR rule) — factrix.stats.holm_adjusted_p is the public API
    # (factrix>=0.17), not a hand-rolled Holm-Bonferroni.
    p_2sided = [min(2 * min(p, 1 - p), 1.0) for _, _, p in horizon_rows]
    p_holm = holm_adjusted_p(p_2sided)
    print(f"\nHolm-corrected (FWER, n={len(p_2sided)} tests, two-sided, alpha={ALPHA}):")
    winners = []
    for (h, hit, p_raw), p2, ph in zip(horizon_rows, p_2sided, p_holm):
        verdict = "PASS" if ph < ALPHA else "fail"
        print(f"  h={h:>3} hit={hit:.4f} p_raw={p_raw:.4f} p_2sided={p2:.4f} p_holm={ph:.4f}  {verdict}")
        if ph < ALPHA:
            winners.append((h, hit, p_raw, ph))

    best = min(horizon_rows, key=lambda r: min(r[2], 1 - r[2]))
    best_h, best_hit, best_p = best
    effect = "reversal" if best_hit < 0.5 else "trend"
    print(f"\nMost extreme (not necessarily significant): h={best_h} hit={best_hit:.4f} p_raw={best_p:.4f} effect={effect}")

    print("\n=== Factor stability: oos_decay (IS-Train internal 70/30 split) ===")
    data_h = compute_forward_return(pl_train, forward_periods=best_h)
    value_series = data_h.select(pl.col("date"), (pl.col("bb_pct_b") * pl.col("forward_return")).alias("value")).drop_nulls().sort("date")
    decay = oos_decay(value_series)
    print(f"h={best_h}: survival_ratio={decay.value:.4f} sign_flipped={decay.metadata.get('sign_flipped')} status={decay.metadata.get('status')}")

    print("\n=== Regime slice (descriptive only, IS-Train): bb_pct_b hit rate by Vol+Amp / OI consolidating ===")
    train_regime = splits["IS-Train"].assign(consolidating_regime=splits["IS-Train"]["is_consolidating"].astype(str))
    train_regime_idx = train_regime.index.tz_localize(None) if train_regime.index.tz is not None else train_regime.index
    pl_regime = _bb_panel(train_regime, DECISION_ASSET).join(
        pl.from_pandas(pd.DataFrame({"date": train_regime_idx, "consolidating_regime": train_regime["consolidating_regime"].values})),
        on="date", how="inner",
    )
    data_bb_h = compute_forward_return(pl_regime, forward_periods=best_h)
    vol_amp_res = fx.by_slice(data_bb_h, directional_hit_rate(), by="consolidating_regime", factor_col="bb_pct_b", strict=False)
    vol_amp_board = fx.compare(list(vol_amp_res.values()), metrics=["metric"]).with_columns(pl.Series("consolidating_regime", list(vol_amp_res.keys())))
    print(f">>> Vol+Amp: bb_pct_b hit rate by is_consolidating @ h={best_h}:\n{vol_amp_board}")

    train_oi = splits_oi["IS-Train"]
    print(f"OI-consolidating share of BTC IS-Train: {train_oi['oi_consolidating'].mean()*100:.1f}% ({len(train_oi)} rows with OI coverage)")
    train_oi_regime = train_oi.assign(oi_regime=train_oi["oi_consolidating"].astype(str))
    train_oi_idx = train_oi_regime.index.tz_localize(None) if train_oi_regime.index.tz is not None else train_oi_regime.index
    pl_oi = _bb_panel(train_oi_regime, DECISION_ASSET).join(
        pl.from_pandas(pd.DataFrame({"date": train_oi_idx, "oi_regime": train_oi_regime["oi_regime"].values})),
        on="date", how="inner",
    )
    data_bb_oi_h = compute_forward_return(pl_oi, forward_periods=best_h)
    oi_res = fx.by_slice(data_bb_oi_h, directional_hit_rate(), by="oi_regime", factor_col="bb_pct_b", strict=False)
    oi_board = fx.compare(list(oi_res.values()), metrics=["metric"]).with_columns(pl.Series("oi_regime", list(oi_res.keys())))
    print(f">>> OI: bb_pct_b hit rate by oi_consolidating @ h={best_h}:\n{oi_board}")

    print("\n=== Candidate comparison (IS-Val, real engine) ===")
    val_results = {}
    for cand in CANDIDATES:
        frame = _candidate_frame(cand, splits["IS-Val"], splits_oi["IS-Val"])
        stats = run_engine_backtest(frame, _RangeOscillatorCandidate(), DECISION_ASSET)
        val_results[cand["name"]] = stats
        print(f"  {cand['name']:30s} {stats}")

    winner_name = max(val_results, key=lambda n: val_results[n]["sharpe"])
    winner_cand = next(c for c in CANDIDATES if c["name"] == winner_name)
    print(f"\nIS-Val winner: {winner_name} (Sharpe={val_results[winner_name]['sharpe']:.4f}) — selected for OOS blind test")

    print("\n=== Blind OOS test (all 4 shown for transparency; selection above used IS-Val only) ===")
    oos_results = {}
    for cand in CANDIDATES:
        frame = _candidate_frame(cand, splits["OOS"], splits_oi["OOS"])
        stats = run_engine_backtest(frame, _RangeOscillatorCandidate(), DECISION_ASSET)
        oos_results[cand["name"]] = stats
        print(f"  {cand['name']:30s} {stats}")

    print(f"\n=== MAE/MFE distribution (winner candidate '{winner_name}', IS-Train) ===")
    is_frame = _candidate_frame(winner_cand, splits["IS-Train"], splits_oi["IS-Train"])
    is_stats = run_engine_backtest(is_frame, _RangeOscillatorCandidate(), DECISION_ASSET, return_trades=True)
    mfe_mae = mae_mfe_percentiles(is_stats["trade_list"], is_frame)
    print(f"  {mfe_mae}")

    print(f"\n=== Cross-asset robustness: {ROBUST_ASSET}, same params, no re-tuning ===")
    eth_raw = _fetch(ROBUST_ASSET, IS_TRAIN_START, OOS_END).set_index("timestamp")
    eth_base = merge_daily_trend(compute_features(eth_raw), eth_raw)
    eth_oi = attach_oi_regime(eth_base, ROBUST_ASSET, IS_TRAIN_START, OOS_END)
    print(f"OI-consolidating share of full ETH window: {eth_oi['oi_consolidating'].mean()*100:.1f}% ({len(eth_oi)}/{len(eth_base)} rows with OI coverage)")
    eth_results = {}
    for cand in ({"name": "No filter (baseline)", "use_vol_amp_filter": False, "use_trend_filter": False, "needs_oi": False, "consolidating_col": "is_consolidating"}, winner_cand):
        frame = _candidate_frame(cand, eth_base, eth_oi)
        stats = run_engine_backtest(frame, _RangeOscillatorCandidate(), ROBUST_ASSET)
        eth_results[cand["name"]] = stats
        print(f"  {cand['name']:30s} {stats}")


if __name__ == "__main__":
    main()
