"""Factor validation for TrendPullback M5 (BTCUSDT M5, M30 gate) — same
purpose as trendpullback/factor_research.py, run so the H1 vs M5 comparison
(see strategies/trendpullback/report.md's merge note) is based on real
results for both, not an assumption that M5 is fine because it was never
tested.

Shorter window than the H1 study (M5 bar density means ~9 months already
gives comparable event counts to the H1 study's ~2.5 years) — kept
tractable given ccxt's paginated public REST fetch with no DB cache here.

Run: ``python -m strategies.trendpullback_m5.factor_research``
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
)
from strategies.data.utils import resample_ohlcv

DECISION_ASSET = "BTCUSDT"

# ② 樣本切分 — shorter window than H1 study; M5 bar density compensates.
IS_TRAIN_START, IS_TRAIN_END = "2025-08-01", "2025-12-31"
IS_VAL_END = "2026-03-31"
OOS_END = "2026-07-01"

# Deployed holding horizon (max_hold_periods=24 M5 bars = 2h).
FORWARD_PERIODS = 24


def _fetch(symbol: str, start: str, end: str) -> pd.DataFrame:
    return get_ohlcv(symbol, "5m", data_source="binance_spot", start=start, end=end, warmup_periods=720)


def _prepare_with_gate(m5_base: pd.DataFrame) -> pd.DataFrame:
    m5 = compute_features(m5_base)
    m30 = resample_ohlcv(m5_base, "30min")
    if len(m30) >= 20:
        m30 = compute_daily_gate(m30)
        m5 = merge_trend_gate(m5, m30)
    else:
        m5["daily_trend"] = True
    m5["entry_signal"] = compute_entry_conditions(m5).values
    m5["exit_signal"] = compute_exit_conditions(m5).values
    return m5


def _prepare_no_gate(m5_base: pd.DataFrame) -> pd.DataFrame:
    m5 = compute_features(m5_base)
    m5["daily_trend"] = True
    m5["entry_signal"] = compute_entry_conditions(m5).values
    m5["exit_signal"] = compute_exit_conditions(m5).values
    return m5


def _test_hit_rate(m5: pd.DataFrame, symbol: str, label: str) -> dict:
    n_events = int(m5["entry_signal"].sum())
    if n_events < 8:
        return {"label": label, "n_events": n_events, "hit_rate": float("nan"), "p_raw": float("nan")}
    panel = build_event_panel(m5, symbol)
    panel = compute_forward_return(panel, forward_periods=FORWARD_PERIODS)
    result = event_hit_rate(panel, factor_col="factor", return_col="forward_return")
    return {"label": label, "n_events": n_events, "hit_rate": result.value, "p_raw": result.p_value}


def _run_backtest(m5: pd.DataFrame, symbol: str, max_hold_periods: int = 24) -> dict:
    from strategies.trendpullback.strategy import TrendPullbackStrategy

    df = m5.copy()
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
    return {"trades": m.trades, "sharpe": m.sharpe, "max_drawdown": m.max_drawdown, "total_return": m.total_return}


def main() -> None:
    print(f"Fetching {DECISION_ASSET} M5 {IS_TRAIN_START}..{OOS_END}...")
    full = _fetch(DECISION_ASSET, IS_TRAIN_START, OOS_END).set_index("timestamp")
    splits = split_is_val_oos(full, IS_TRAIN_END, IS_VAL_END, OOS_END)

    print("\n=== ③ Event hit-rate: with-gate vs no-gate ablation ===")
    rows = []
    for split_name, split_df in splits.items():
        for variant, prep_fn in (("with_gate", _prepare_with_gate), ("no_gate", _prepare_no_gate)):
            m5 = prep_fn(split_df)
            row = _test_hit_rate(m5, DECISION_ASSET, f"{split_name}/{variant}")
            rows.append(row)
            print(f"  {row['label']:22s} n_events={row['n_events']:5d}  hit_rate={row['hit_rate']:.4f}  p_raw={row['p_raw']:.4f}")
    print_holm_corrected(rows)

    print("\n=== ⑦ Real-engine backtest (zero cost, IS-Train+IS-Val) ===")
    fit_window = full.loc[IS_TRAIN_START:IS_VAL_END]
    for variant, prep_fn in (("with_gate", _prepare_with_gate), ("no_gate", _prepare_no_gate)):
        stats = _run_backtest(prep_fn(fit_window), DECISION_ASSET)
        print(f"  {variant:10s} {stats}")

    print("\n=== ⑦ OOS blind check ===")
    for variant, prep_fn in (("with_gate", _prepare_with_gate), ("no_gate", _prepare_no_gate)):
        stats = _run_backtest(prep_fn(splits["OOS"]), DECISION_ASSET)
        print(f"  {variant:10s} {stats}")


if __name__ == "__main__":
    main()
