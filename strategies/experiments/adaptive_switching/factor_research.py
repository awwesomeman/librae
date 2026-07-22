"""Factor validation for Adaptive Switching — regime-conditional selection
between a momentum (breakout) sub-strategy and an RSI (mean-reversion)
sub-strategy, gated by a volatility regime.

Ported from a different project's ``utils/`` (``utils.data``,
``utils.cached_kline``, ``utils.universe``, ``utils.factors``, ``utils.stats``,
``utils.engine_check``, ``utils.mfe_mae``, ``utils.backtest_sim`` — none of
which exist in this repo) onto this repo's actual tools:
``strategies.module.data.ohlcv.get_ohlcv`` (not ``utils/data.py``/
``utils/cached_kline.py``), ``librae.backtest.engine.Backtest`` via
``strategies.module.factors.utils.run_engine_backtest`` (not a hand-rolled
``simulate_switching``/``BacktestService``), ``factrix.stats.holm_adjusted_p``
(not a hand-rolled Holm-Bonferroni — factrix now ships a public FWER-control
API, see RESEARCH_METHODOLOGY.md ).

The regime switch itself is redefined: the legacy script used a bespoke
"cumulative intraday volume since UTC midnight vs its own 30-day hour-of-day
average" ratio (``vol_ratio > 1.15``), which assumes a 24/7 market and
crashed on non-continuous sessions. This version uses the shared
``strategies.module.data.regime.compute_vol_regime`` (ATR-ratio-vs-rolling-
baseline high_vol/low_vol classifier) instead — same hypothesis (switch
sub-strategy by a volatility read), a shared, already-tested, no-look-ahead
implementation. See ``utils.py`` module docstring.

No ``strategy.py`` exists for this family — see report.md's conclusion.

Run: ``python -m strategies.experiments.adaptive_switching.factor_research``
"""
from __future__ import annotations

import numpy as np
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
from strategies.experiments.adaptive_switching.utils import prepare_signals

DECISION_ASSET = "BTCUSDT"
ROBUST_ASSET = "ETHUSDT"

# Same H1 sample split as trendpullback/mtf_trend_rsi's factor_research.py —
# all three families validate on BTCUSDT H1, keeping the reports directly
# comparable.
IS_TRAIN_START, IS_TRAIN_END = "2024-01-01", "2024-12-31"
IS_VAL_END = "2025-08-31"
OOS_END = "2026-07-01"

HORIZONS = [1, 4, 12, 24]
ALPHA = 0.05


class _AdaptiveSwitchingCandidate(BaseStrategy):
    """Candidate only — not exported, not a deployed strategy. Reads the
    long/short entry+exit booleans ``utils.prepare_signals`` computes (same
    on_bar shape as ``mtf_trend_rsi``'s candidate — both read pre-computed
    long_entry/short_entry/long_exit/short_exit)."""

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


def _to_panel(df: pd.DataFrame, symbol: str, factor_cols: list[str]) -> pl.DataFrame:
    out = pd.DataFrame({"date": df.index.tz_localize(None) if df.index.tz is not None else df.index, "asset_id": symbol})
    for c in factor_cols:
        out[c] = df[c].values
    out["price"] = df["close"].values
    return pl.from_pandas(out).drop_nulls()


def _best_horizon(rows: list[tuple], factor: str) -> tuple:
    """Most extreme p-value away from 0.5 in EITHER direction — this factor
    set is tested for both a trend effect (hit rate near 1) and a reversion
    effect (hit rate near 0)."""
    matches = [r for r in rows if r[0] == factor]
    return min(matches, key=lambda r: min(r[3], 1 - r[3]))


def _run_backtests(full: pd.DataFrame, splits: dict, symbol: str, label: str) -> None:
    print(f"\n=== Backtest: IS-Val candidate comparison ({label}) ===")
    for mode in ("momentum", "rsi", "switch"):
        df = prepare_signals(splits["IS-Val"], mode=mode)
        stats = run_engine_backtest(df, _AdaptiveSwitchingCandidate(), symbol)
        print(f"  {mode:10s} {stats}")

    print(f"\n=== Backtest: OOS blind check ({label}) ===")
    for mode in ("momentum", "rsi", "switch"):
        df = prepare_signals(splits["OOS"], mode=mode)
        stats = run_engine_backtest(df, _AdaptiveSwitchingCandidate(), symbol)
        print(f"  {mode:10s} {stats}")


def main() -> None:
    print(f"Fetching {DECISION_ASSET} H1 {IS_TRAIN_START}..{OOS_END}...")
    full = _fetch(DECISION_ASSET, IS_TRAIN_START, OOS_END).set_index("timestamp")
    splits = split_is_val_oos(full, IS_TRAIN_END, IS_VAL_END, OOS_END)

    print("\n=== Factor significance: mom_1h / rsi_demeaned, multi-horizon sweep (IS-Train) ===")
    train_feat = prepare_signals(splits["IS-Train"], mode="rsi")  # mode irrelevant here, just need rsi/mom_1h columns
    train_feat["rsi_demeaned"] = train_feat["rsi"] - 50.0
    panel_train = _to_panel(train_feat, DECISION_ASSET, ["mom_1h", "rsi_demeaned"])

    sweep_results = fx.evaluate_horizons(
        panel_train, metrics={"dir_hit": directional_hit_rate()},
        factor_cols=["mom_1h", "rsi_demeaned"], forward_periods=HORIZONS,
    )
    horizon_rows = []
    print(f"{'factor':<14} | {'h':>3} | {'hit_rate':>8} | {'p_value':>8}")
    for r in sweep_results:
        m = r.metrics["dir_hit"]
        horizon_rows.append((r.factor, r.forward_periods, m.value, m.p_value))
        print(f"{r.factor:<14} | {r.forward_periods:>3} | {m.value:>8.4f} | {m.p_value:>8.4f}")

    # two-sided transform BEFORE correcting (holm_adjusted_p expects a
    # small-p-is-significant statistic) — see mtf_trend_rsi/RESEARCH_METHODOLOGY.md
    # for why correcting raw one-sided p and re-checking min(p,1-p)
    # afterwards would be wrong (it would flag p~0.97 rows as significant
    # without ever passing through the correction).
    p_raw = [r[3] for r in horizon_rows]
    p_2sided = [min(2 * min(p, 1 - p), 1.0) for p in p_raw]
    p_holm = holm_adjusted_p(p_2sided)
    print(f"\nHolm-corrected (FWER, n={len(p_raw)} tests, two-sided, alpha={ALPHA}):")
    winners = []
    for row, p_adj in zip(horizon_rows, p_holm):
        verdict = "PASS" if p_adj < ALPHA else "fail"
        print(f"  {row[0]:<14} h={row[1]:>3} hit={row[2]:.4f} p_holm={p_adj:.4f}  {verdict}")
        if p_adj < ALPHA:
            winners.append((*row, p_adj))
    horizon_rows_adj = [(*row, p_adj) for row, p_adj in zip(horizon_rows, p_holm)]

    mom_best = _best_horizon(horizon_rows, "mom_1h")
    rsi_best = _best_horizon(horizon_rows, "rsi_demeaned")
    mom_effect = "reversal" if mom_best[2] < 0.5 else "trend"
    rsi_effect = "reversal" if rsi_best[2] < 0.5 else "trend"
    print(f"\nmom_1h most stable at h={mom_best[1]} (hit={mom_best[2]:.4f}, p={mom_best[3]:.4f}, effect={mom_effect})")
    print(f"rsi_demeaned most stable at h={rsi_best[1]} (hit={rsi_best[2]:.4f}, p={rsi_best[3]:.4f}, effect={rsi_effect})")

    print("\n=== b Factor margin stability (oos_decay, IS-Train internal 70/30 split) ===")
    decay_rows = []
    for factor, best in [("mom_1h", mom_best), ("rsi_demeaned", rsi_best)]:
        h = best[1]
        data_h = compute_forward_return(panel_train, forward_periods=h)
        value_series = data_h.select(
            pl.col("date"), (pl.col(factor) * pl.col("forward_return")).alias("value"),
        ).drop_nulls().sort("date")
        decay = oos_decay(value_series)
        decay_rows.append((factor, h, decay.value, decay.metadata.get("sign_flipped"), decay.metadata.get("status")))
        print(f"  {factor} @ h={h}: survival_ratio={decay.value:.4f} sign_flipped={decay.metadata.get('sign_flipped')} status={decay.metadata.get('status')}")

    print("\n=== c Vol-regime slice significance (IS-Train, factrix by_slice) ===")
    train_regime = train_feat[["mom_1h", "rsi_demeaned", "vol_regime"]].copy()
    train_regime.index = train_feat.index
    panel_regime = _to_panel(train_regime.assign(close=train_feat["close"]), DECISION_ASSET, ["mom_1h", "rsi_demeaned", "vol_regime"])
    panel_regime = panel_regime.with_columns(pl.col("vol_regime").cast(pl.Utf8))

    regime_rows = []
    for factor, best in [("mom_1h", mom_best), ("rsi_demeaned", rsi_best)]:
        h = best[1]
        data_h = compute_forward_return(panel_regime, forward_periods=h)
        board_res = fx.by_slice(data_h, directional_hit_rate(), by="vol_regime", factor_col=factor, strict=False)
        for regime_key, res in board_res.items():
            m = res.metrics["metric"] if hasattr(res, "metrics") else res
            regime_rows.append((factor, h, regime_key, m.value, m.p_value))
            print(f"  {factor} @ h={h} | vol_regime={regime_key}: hit={m.value:.4f} p={m.p_value:.4f}")

    print("\n=== Frequency/holding decision ===")
    freq_note = (
        f"mom_1h most stable at h={mom_best[1]} (hit={mom_best[2]:.4f}, p={mom_best[3]:.4f}, effect={mom_effect}); "
        f"rsi_demeaned most stable at h={rsi_best[1]} (hit={rsi_best[2]:.4f}, p={rsi_best[3]:.4f}, effect={rsi_effect}). "
        f"Deployed logic evaluates entries/exits every H1 bar regardless of which horizon the sweep favors — "
        f"recorded here as a known gap, not retroactively changed (see conclusion)."
    )
    print(freq_note)

    _run_backtests(full, splits, DECISION_ASSET, "BTCUSDT")

    print("\n=== MAE/MFE distribution (switch mode, IS-Train entries) ===")
    is_train_df = prepare_signals(splits["IS-Train"], mode="switch")
    stats = run_engine_backtest(is_train_df, _AdaptiveSwitchingCandidate(), DECISION_ASSET, return_trades=True)
    mfe_mae = mae_mfe_percentiles(stats["trade_list"], is_train_df)
    print(f"  {mfe_mae}")

    print(f"\n=== /Cross-asset robustness: {ROBUST_ASSET}, same params, no re-tuning ===")
    eth_full = _fetch(ROBUST_ASSET, IS_TRAIN_START, OOS_END).set_index("timestamp")
    eth_splits = split_is_val_oos(eth_full, IS_TRAIN_END, IS_VAL_END, OOS_END)
    _run_backtests(eth_full, eth_splits, ROBUST_ASSET, "ETHUSDT")


if __name__ == "__main__":
    main()
