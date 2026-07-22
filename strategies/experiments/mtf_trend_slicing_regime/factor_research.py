"""Factor validation for MTF Trend Slicing Regime — re-validates, on this
repo's own data/engine, the "does a Fear & Greed / DXY sentiment filter add
value over a no-filter baseline" question this family's originally-researched
logic tested (a different project's utils, not runnable in this repo — see
old ``factor_slicing_research.py``, deleted; its conclusion is quoted in
``strategies/FACTOR_ANALYSIS.md``: "IS-Val、OOS 排序不一致（互相矛盾），濾鏡
加值與否不穩定，不可靠").

Simplifications vs. the original script (see report.md's "研究設計備註" for
the full rationale):
  - The original /c tested a ``mom_1H_12`` factor that the deployed
    strategy never actually used (the deployed trend gate is ``mom_1D_10``,
    tested nowhere in the original). This rewrite validates the
    actually-deployed factors only (``mom_1D_10``, ``rsi_1H_14``), matching
    ``mtf_trend_rsi``/``trendpullback``'s convention of validating what's
    used, not an orphan research-only factor.
  - Regime slicing (c) is done for ``rsi_1H_14`` only (the entry/exit
    trigger) across all three regime axes (fng/dxy/vol) — ``mom_1D_10``
    lives on D1 and merging it continuously onto H1 isn't needed to answer
    this study's core question.
  - TXFR1 (the original's third robustness asset) is dropped — its FNG/DXY
    fallback is neutral by construction (see ``regime.py``'s docstring), so
    it never actually exercises the sentiment filter under test.

No ``strategy.py`` exists for this family yet — see report.md's conclusion
for whether this validation passed.

Run: ``python -m strategies.experiments.mtf_trend_slicing_regime.factor_research``
"""
from __future__ import annotations

import pandas as pd
import polars as pl
from factrix import by_slice, compare, evaluate_horizons
from factrix.metrics import directional_hit_rate, oos_decay
from factrix.preprocess import compute_forward_return

from librae.core.strategy import Action, BaseStrategy, Context
from strategies.module.data.ohlcv import get_ohlcv
from strategies.module.data.regime import attach_regime_columns
from strategies.module.data.utils import resample_ohlcv
from strategies.module.factors.operators import momentum
from strategies.module.factors.utils import (
    mae_mfe_percentiles,
    print_holm_corrected,
    run_engine_backtest,
    test_event_hit_rate,
)
from strategies.module.utils import split_is_val_oos
from strategies.experiments.mtf_trend_slicing_regime.utils import prepare_signals

DECISION_ASSET = "BTCUSDT"
ROBUST_ASSET = "ETHUSDT"

# Same H1 sample split as trendpullback/mtf_trend_rsi — keeps all three
# reports directly comparable.
IS_TRAIN_START, IS_TRAIN_END = "2024-01-01", "2024-12-31"
IS_VAL_END = "2025-08-31"
OOS_END = "2026-07-01"

HORIZONS = [1, 4, 12, 24]
ENTRY_FORWARD_PERIODS = 24
MOM_FORWARD_DAYS = 10


class _MTFTrendSlicingRegimeCandidate(BaseStrategy):
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
    raw = get_ohlcv(symbol, "1h", data_source="binance_spot", start=start, end=end, warmup_periods=720)
    return attach_regime_columns(raw, is_crypto=True, start=start, end=end)


def _mom_1d_10_hit_rate(h1: pd.DataFrame, symbol: str, label: str) -> dict:
    """Continuous-factor significance: does mom_1D_10's sign predict the
    next MOM_FORWARD_DAYS-day direction?"""
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
    panel = compute_forward_return(panel, forward_periods=MOM_FORWARD_DAYS)
    result = directional_hit_rate(panel, factor_col="factor", return_col="forward_return")
    return {"label": label, "n_events": int(n_valid), "hit_rate": result.value, "p_raw": result.p_value}


def _short_hit_rate(h1: pd.DataFrame, symbol: str, label: str, forward_periods: int) -> dict:
    """short_entry's success means price falls — invert the price series
    before feeding test_event_hit_rate() so its "positive forward return"
    check ends up testing "did price actually decline"."""
    inverted = h1.copy()
    inverted["close"] = -inverted["close"]
    return test_event_hit_rate(inverted, symbol, label, forward_periods, signal_col="short_entry")


def slice_leaderboard(data: pl.DataFrame, by: str, factor_col: str) -> pl.DataFrame:
    """by_slice + compare: descriptive (non-test) leaderboard for a
    categorical regime slice. Deliberately not slice_pairwise_test/
    slice_joint_test — directional_hit_rate is a TS_ONLY metric on a single
    asset's own time axis, not a cross-sectional metric with multiple
    assets per date, so a formal cross-slice test doesn't structurally
    apply here (same reasoning the original research used)."""
    res = by_slice(data, directional_hit_rate(), by=by, factor_col=factor_col, strict=False)
    keys = list(res.keys())
    return compare(list(res.values()), metrics=["metric"]).with_columns(pl.Series(by, keys))


def main() -> None:
    print(f"=== 資產/資料層: {DECISION_ASSET} H1, {IS_TRAIN_START}..{OOS_END} ===")
    full = _fetch(DECISION_ASSET, IS_TRAIN_START, OOS_END).set_index("timestamp")
    splits = split_is_val_oos(full, IS_TRAIN_END, IS_VAL_END, OOS_END)
    for name, df in splits.items():
        print(f"  {name}: {len(df)} rows")

    print("\n=== 因子顯著性: mom_1D_10（連續因子，方向性 hit rate） ===")
    mom_rows = []
    for split_name, split_df in splits.items():
        row = _mom_1d_10_hit_rate(split_df, DECISION_ASSET, split_name)
        mom_rows.append(row)
        print(f"  {row['label']:10s} n={row['n_events']:4d}  hit_rate={row['hit_rate']:.4f}  p_raw={row['p_raw']:.4f}")
    print_holm_corrected(mom_rows)

    print("\n=== 因子顯著性: rsi_1H_14 多頻率橫掃（IS-Train, factrix evaluate_horizons） ===")
    # RSI is bounded [0, 100] with no natural zero-crossing — directional_hit_rate
    # predicts sign(factor), so the factor must be demeaned (RSI - 50) for this
    # significance test only; the deployed threshold logic (utils.py) stays on
    # the raw 0-100 scale (threshold-crossing, not a sign test).
    is_train = prepare_signals(splits["IS-Train"], {"use_filter": True})
    pl_train = pl.from_pandas(
        is_train.reset_index()[["timestamp", "rsi"]].rename(columns={"timestamp": "date"})
    ).with_columns(
        (pl.col("rsi") - 50.0).alias("rsi_1H_14"),
        pl.lit(DECISION_ASSET).alias("asset_id"),
        pl.Series("price", is_train["close"].values),
    ).drop("rsi")
    sweep_results = evaluate_horizons(
        pl_train, metrics={"dir_hit": directional_hit_rate()},
        factor_cols=["rsi_1H_14"], forward_periods=HORIZONS, strict=False,
    )
    rsi_sweep_rows = []
    print(f"  {'h(bars)':>7} | {'hit_rate':>8} | {'p_raw':>8}")
    for r in sweep_results:
        m = r.metrics["dir_hit"]
        if m.value is None or m.p_value is None:
            print(f"  {r.forward_periods:>7} | (inapplicable — {m.metadata.get('reason', 'n/a')})")
            continue
        rsi_sweep_rows.append((r.forward_periods, m.value, m.p_value))
        print(f"  {r.forward_periods:>7} | {m.value:>8.4f} | {m.p_value:>8.4f}")

    # p_raw is one-sided (P(Z > S_n), large S_n = trend). "Most stable
    # significant horizon in EITHER direction" (trend or reversal) needs a
    # two-sided transform before ranking/correcting — same reasoning the
    # original research used for this specific step.
    rsi_sweep_p2 = [min(2 * min(p, 1 - p), 1.0) for _, _, p in rsi_sweep_rows]
    best_idx = min(range(len(rsi_sweep_p2)), key=lambda i: rsi_sweep_p2[i])
    rsi_best_h, rsi_best_hit, rsi_best_p = rsi_sweep_rows[best_idx]
    rsi_effect = "reversal" if rsi_best_hit < 0.5 else "trend"
    print(f"\n  最穩定顯著: {rsi_best_h}h (hit={rsi_best_hit:.4f}, p={rsi_best_p:.4f}, 效應方向={rsi_effect})")
    print_holm_corrected([
        {"label": f"rsi_1H_14@{h}h", "p_raw": p2} for (h, _, _), p2 in zip(rsi_sweep_rows, rsi_sweep_p2)
    ])

    print("\n=== b rsi_1H_14 邊際穩定性（oos_decay，IS-Train 內部 70/30 切分） ===")
    data_h = compute_forward_return(pl_train, forward_periods=rsi_best_h)
    value_series = data_h.select(
        pl.col("date"), (pl.col("rsi_1H_14") * pl.col("forward_return")).alias("value"),
    ).drop_nulls().sort("date")
    decay = oos_decay(value_series)
    print(f"  survival_ratio={decay.value:.4f} sign_flipped={decay.metadata.get('sign_flipped')} status={decay.metadata.get('status')}")

    print("\n=== c Regime 切片檢定（IS-Train, rsi_1H_14 @ best horizon, factrix by_slice+compare） ===")
    pl_train_regime = pl.from_pandas(
        is_train.reset_index()[["timestamp", "close", "rsi", "fng_regime", "dxy_trend", "vol_regime"]]
        .rename(columns={"timestamp": "date", "close": "price"})
    ).with_columns(
        (pl.col("rsi") - 50.0).alias("rsi_1H_14"),
        pl.lit(DECISION_ASSET).alias("asset_id"),
    ).drop("rsi")
    data_regime_h = compute_forward_return(pl_train_regime, forward_periods=rsi_best_h)

    for by_col in ("fng_regime", "dxy_trend", "vol_regime"):
        board = slice_leaderboard(data_regime_h, by_col, "rsi_1H_14")
        print(f"\n  >>> sliced by {by_col}:")
        print(board)

    print("\n=== 頻率/持有期決定 ===")
    print(
        f"rsi_1H_14 在 {rsi_best_h}h 最穩定顯著（效應方向: {rsi_effect}）。部署邏輯是逐 1H bar 判斷、"
        f"門檻觸發才出場（非固定持有期），跟橫掃出的最適 forward period 未必一致——這裡如實記錄落差，"
        f"不回頭改動部署邏輯（見結論）。"
    )

    print("\n=== d 部署訊號事件顯著性: with_filter vs no_filter（factrix event_hit_rate） ===")
    entry_rows = []
    for split_name, split_df in splits.items():
        for variant, use_filter in (("with_filter", True), ("no_filter", False)):
            df = prepare_signals(split_df, {"use_filter": use_filter})
            long_row = test_event_hit_rate(df, DECISION_ASSET, f"{split_name}/{variant}/long", ENTRY_FORWARD_PERIODS, signal_col="long_entry")
            short_row = _short_hit_rate(df, DECISION_ASSET, f"{split_name}/{variant}/short", ENTRY_FORWARD_PERIODS)
            entry_rows += [long_row, short_row]
            for row in (long_row, short_row):
                print(f"  {row['label']:28s} n_events={row['n_events']:5d}  hit_rate={row['hit_rate']:.4f}  p_raw={row['p_raw']:.4f}")
    print_holm_corrected(entry_rows)

    print("\n=== 策略候選比較（IS-Val, zero-cost engine backtest） ===")
    fit_window = full.loc[IS_TRAIN_START:IS_VAL_END]
    val_results = {}
    for variant, use_filter in (("with_filter", True), ("no_filter", False)):
        df = prepare_signals(fit_window, {"use_filter": use_filter})
        stats = run_engine_backtest(df, _MTFTrendSlicingRegimeCandidate(), DECISION_ASSET)
        val_results[variant] = stats
        print(f"  {variant:12s} {stats}")

    print("\n=== b 盲測 OOS ===")
    oos_results = {}
    for variant, use_filter in (("with_filter", True), ("no_filter", False)):
        df = prepare_signals(splits["OOS"], {"use_filter": use_filter})
        stats = run_engine_backtest(df, _MTFTrendSlicingRegimeCandidate(), DECISION_ASSET)
        oos_results[variant] = stats
        print(f"  {variant:12s} {stats}")

    filter_val_better = val_results["with_filter"]["sharpe"] > val_results["no_filter"]["sharpe"]
    filter_oos_better = oos_results["with_filter"]["sharpe"] > oos_results["no_filter"]["sharpe"]
    print(f"\n  filter beats baseline on IS-Val: {filter_val_better} | on OOS: {filter_oos_better} | "
          f"consistent: {filter_val_better == filter_oos_better}")

    print("\n=== MAE/MFE 分布（IS-Train, with_filter） ===")
    is_train_filtered = prepare_signals(splits["IS-Train"], {"use_filter": True})
    is_stats = run_engine_backtest(is_train_filtered, _MTFTrendSlicingRegimeCandidate(), DECISION_ASSET, return_trades=True)
    print(f"  {mae_mfe_percentiles(is_stats['trade_list'], is_train_filtered)}")

    print(f"\n=== 跨資產穩健性: {ROBUST_ASSET}, 同參數不重調 ===")
    eth_full = _fetch(ROBUST_ASSET, IS_TRAIN_START, OOS_END).set_index("timestamp")
    eth_results = {}
    for variant, use_filter in (("with_filter", True), ("no_filter", False)):
        df = prepare_signals(eth_full, {"use_filter": use_filter})
        stats = run_engine_backtest(df, _MTFTrendSlicingRegimeCandidate(), ROBUST_ASSET)
        eth_results[variant] = stats
        print(f"  {variant:12s} {stats}")


if __name__ == "__main__":
    main()
