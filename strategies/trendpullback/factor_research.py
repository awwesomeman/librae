"""Retroactive factor validation for TrendPullback (BTCUSDT H1) — filling
the gap this family was deployed with (see report.md's opening note):
no factor-significance step was ever run before this strategy went live.

Follows the ①~⑧ pipeline documented in strategies/experiments/README.md's
RESEARCH_METHODOLOGY.md, mapped onto this repo's actual tools:
``strategies.data.ohlcv.get_ohlcv`` (not ``utils/data.py``), ``librae.backtest.engine.Backtest``
(not ``BacktestService``) — no separate hand-rolled simulator needed since
this repo's real backtest engine is directly runnable here.

Run: ``python -m strategies.trendpullback.factor_research``
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from factrix.metrics.event_quality import event_hit_rate
from factrix.preprocess import compute_forward_return

from librae.backtest.engine import Backtest
from librae.core.cost_model import CostModel
from strategies.data.ohlcv import get_ohlcv
from strategies.module.factor import build_event_panel, print_holm_corrected
from strategies.module.utils import split_is_val_oos
from strategies.trendpullback.utils import (
    compute_daily_gate,
    compute_entry_conditions,
    compute_exit_conditions,
    compute_features,
    merge_trend_gate,
    resample_to_daily,
)

# ① 資產/資料層
DECISION_ASSET = "BTCUSDT"
ROBUST_ASSET = "ETHUSDT"

# ② 樣本切分 — IS-Train (factor test) / IS-Val (candidate compare) / OOS (blind)
IS_TRAIN_START, IS_TRAIN_END = "2024-01-01", "2024-12-31"
IS_VAL_END = "2025-08-31"
OOS_END = "2026-07-01"

# Deployed holding horizon — not swept here; this is validating the factor
# at the horizon the strategy actually uses, not searching for a better one.
FORWARD_PERIODS = 24


def _fetch(symbol: str, start: str, end: str) -> pd.DataFrame:
    return get_ohlcv(symbol, "1h", data_source="binance_spot", start=start, end=end, warmup_periods=720)


def _prepare_with_gate(h1_base: pd.DataFrame) -> pd.DataFrame:
    """Deployed logic (post-fix): daily_trend gate shifted by 1 D1 bar."""
    h1 = compute_features(h1_base)
    d1 = resample_to_daily(h1_base)
    if len(d1) >= 20:
        d1 = compute_daily_gate(d1)
        h1 = merge_trend_gate(h1, d1)
    else:
        h1["daily_trend"] = True
    h1["entry_signal"] = compute_entry_conditions(h1).values
    h1["exit_signal"] = compute_exit_conditions(h1).values
    return h1


def _prepare_no_gate(h1_base: pd.DataFrame) -> pd.DataFrame:
    """Ablation baseline — same pullback/volume/bullish-bar conditions,
    daily_trend forced True (⑤'s required "no extra filter" baseline)."""
    h1 = compute_features(h1_base)
    h1["daily_trend"] = True
    h1["entry_signal"] = compute_entry_conditions(h1).values
    h1["exit_signal"] = compute_exit_conditions(h1).values
    return h1


def _test_hit_rate(h1: pd.DataFrame, symbol: str, label: str) -> dict:
    n_events = int(h1["entry_signal"].sum())
    if n_events < 8:
        return {"label": label, "n_events": n_events, "hit_rate": float("nan"), "p_raw": float("nan")}

    panel = build_event_panel(h1, symbol)
    panel = compute_forward_return(panel, forward_periods=FORWARD_PERIODS)
    result = event_hit_rate(panel, factor_col="factor", return_col="forward_return")
    return {"label": label, "n_events": n_events, "hit_rate": result.value, "p_raw": result.p_value}


def _mae_mfe_percentiles(trades, h1: pd.DataFrame) -> dict:
    """MAE/MFE distribution across trades — max adverse/favorable excursion
    between entry_at and exit_at, relative to entry_price (%). Long-only,
    so MFE uses the window high, MAE the window low.

    Matches the SL/TP-calibration convention used by every other
    strategies/experiments/*/report.md (⑥): SL candidate = P75(|MAE|),
    TP candidate = P50(MFE) — reported here as a distribution for
    reference, not applied as an overlay (this family has no fixed SL/TP;
    see report.md's ⑥ note for why).
    """
    if not trades:
        return {"n": 0}
    mfes, maes = [], []
    for t in trades:
        window = h1.loc[t.entry_at:t.exit_at]
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


def _run_backtest(h1: pd.DataFrame, symbol: str, max_hold_periods: int = 24) -> dict:
    """Standard 4-metric table used by every strategies/experiments/*/report.md:
    total_return (淨報酬), sharpe, max_drawdown (最大回撤), trades (交易次數)."""
    from strategies.trendpullback.strategy import TrendPullbackStrategy

    df = h1.copy()
    df.index.name = "ts"
    mi = pd.MultiIndex.from_arrays([[symbol] * len(df), df.index], names=["symbol", "datetime"])
    df = df.set_index(mi)

    bt = Backtest(
        df, TrendPullbackStrategy(max_hold_periods=max_hold_periods),
        initial_balance=100_000, cost_model=CostModel.zero(), data_source="research",
    )
    bt.run()
    bt.build_output(annualize=True)
    m = bt.metrics

    return {
        "trades": m.trades,
        "sharpe": m.sharpe,
        "max_drawdown": m.max_drawdown,
        "total_return": m.total_return,
    }


def main() -> None:
    print(f"Fetching {DECISION_ASSET} H1 {IS_TRAIN_START}..{OOS_END}...")
    full = _fetch(DECISION_ASSET, IS_TRAIN_START, OOS_END)
    full = full.set_index("timestamp")
    splits = split_is_val_oos(full, IS_TRAIN_END, IS_VAL_END, OOS_END)

    print("\n=== ③ Event hit-rate: with-gate vs no-gate ablation ===")
    rows = []
    for split_name, split_df in splits.items():
        for variant, prep_fn in (("with_gate", _prepare_with_gate), ("no_gate", _prepare_no_gate)):
            h1 = prep_fn(split_df)
            row = _test_hit_rate(h1, DECISION_ASSET, f"{split_name}/{variant}")
            rows.append(row)
            print(f"  {row['label']:22s} n_events={row['n_events']:4d}  hit_rate={row['hit_rate']:.4f}  p_raw={row['p_raw']:.4f}")

    print_holm_corrected(rows)

    print("\n=== ⑦ Real-engine backtest (librae.Backtest, zero cost, IS-Train+IS-Val) ===")
    fit_window = full.loc[IS_TRAIN_START:IS_VAL_END]
    for variant, prep_fn in (("with_gate", _prepare_with_gate), ("no_gate", _prepare_no_gate)):
        h1 = prep_fn(fit_window)
        stats = _run_backtest(h1, DECISION_ASSET)
        print(f"  {variant:10s} {stats}")

    print("\n=== ⑥ MAE/MFE distribution (with_gate, IS-Train only) ===")
    from strategies.trendpullback.strategy import TrendPullbackStrategy
    is_train_h1 = _prepare_with_gate(splits["IS-Train"])
    df = is_train_h1.copy()
    df.index.name = "ts"
    mi = pd.MultiIndex.from_arrays([[DECISION_ASSET] * len(df), df.index], names=["symbol", "datetime"])
    df = df.set_index(mi)
    bt = Backtest(df, TrendPullbackStrategy(max_hold_periods=24), initial_balance=100_000, cost_model=CostModel.zero(), data_source="research")
    result = bt.run()
    mae_mfe = _mae_mfe_percentiles(result.trades, is_train_h1)
    print(f"  {mae_mfe}")

    print("\n=== ⑦ OOS blind check (same params, never used to pick anything above) ===")
    oos_df = splits["OOS"]
    for variant, prep_fn in (("with_gate", _prepare_with_gate), ("no_gate", _prepare_no_gate)):
        h1 = prep_fn(oos_df)
        stats = _run_backtest(h1, DECISION_ASSET)
        print(f"  {variant:10s} {stats}")

    print(f"\n=== ⑧ Cross-asset robustness: {ROBUST_ASSET}, same params, no re-tuning ===")
    eth_full = _fetch(ROBUST_ASSET, IS_TRAIN_START, OOS_END).set_index("timestamp")
    for variant, prep_fn in (("with_gate", _prepare_with_gate), ("no_gate", _prepare_no_gate)):
        h1 = prep_fn(eth_full)
        stats = _run_backtest(h1, ROBUST_ASSET)
        print(f"  {variant:10s} {stats}")


if __name__ == "__main__":
    main()
