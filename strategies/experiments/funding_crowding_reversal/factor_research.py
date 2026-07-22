"""Funding-Rate Crowding Reversal — external-data research track.

Question this script answers: does external data (perpetual funding-rate
positioning + BTC/ETH cross-asset linkage) add anything on top of a
plain-OHLCV baseline, on this repo's actual data/engine tooling?

Rewritten from a different project's ``utils/`` (``utils.data``,
``utils.cached_kline``, ``utils.universe``, ``utils.factors``, ``utils.stats``,
``utils.engine_check``, ``utils.mfe_mae``, ``utils.backtest_sim`` — none of
which exist in this repo, so the original script could not run here) onto:
``strategies.module.data.ohlcv.get_ohlcv`` (real OHLCV, DB-first + ccxt
fallback), ``strategies.module.data.funding`` (Binance perpetual funding rate,
public, no auth), ``strategies.module.data.cross_asset`` (rolling
correlation/relative-momentum), and ``librae.backtest.engine.Backtest`` (the
real engine, via ``strategies.module.factors.utils.run_engine_backtest`` — no
separate hand-rolled simulator or sandboxed-engine wrapper needed: unlike the
original project's ``BacktestService`` sandbox, this repo's engine runs
directly against a plain DataFrame with pre-computed signal columns, so the
external-data candidates (A/B below) get real-engine numbers same as the
OHLCV-only baseline (C) — the original's "can't cross-check A/B in the real
engine" limitation does not apply here).

Same two external ingredients as the original research:
  1. "Chip" proxy: perpetual funding rate (``strategies.module.data.funding``)
     — persistent positive funding = crowded-long leveraged positioning.
  2. Cross-asset linkage: BTC-vs-ETH rolling correlation and relative
     momentum (``strategies.module.data.cross_asset``).

Three candidates compared on IS-Val (from 's screening decides the base
frequency stays H1 — see the section below):
  A. Funding Crowding Reversal — pure contrarian fade of funding_z_3d.
  B. A + cross-asset relative-momentum confirmation filter.
  C. OHLCV-only baseline (daily trend gate + hourly momentum breakout) — no
     external data at all, the required ablation per RESEARCH_METHODOLOGY .

Follows strategies/RESEARCH_METHODOLOGY.md's ~pipeline. Sample split
matches strategies/experiments/trendpullback and mtf_trend_rsi (BTCUSDT H1,
IS-Train 2024-01-01~2024-12-31 / IS-Val ~2025-08-31 / OOS ~2026-07-01) so all
three reports are directly comparable — Binance perpetual funding-rate
history for BTC/ETH covers this window with no gap (funding launched with
the perpetual contracts themselves, years before 2024).

No ``strategy.py`` exists for this family unless conclusion finds a
deployable highlight — see report.md.

Run: ``python -m strategies.experiments.funding_crowding_reversal.factor_research``
"""
from __future__ import annotations

import pandas as pd
import polars as pl
import factrix as fx
from factrix.metrics import directional_hit_rate, oos_decay
from factrix.stats import holm_adjusted_p

from librae.core.strategy import Action, BaseStrategy, Context
from strategies.module.data.ohlcv import get_ohlcv
from strategies.module.data.funding import attach_funding_features
from strategies.module.data.cross_asset import attach_cross_asset_features
from strategies.module.factors.utils import (
    mae_mfe_percentiles,
    print_holm_corrected,
    run_engine_backtest,
    test_event_hit_rate,
)
from strategies.module.utils import split_is_val_oos
from strategies.experiments.funding_crowding_reversal.utils import (
    merge_daily_trend_gate,
    prepare_signals,
)

DECISION_ASSET = "BTCUSDT"
ROBUST_ASSET = "ETHUSDT"
REF_ASSET = {"BTCUSDT": "ETHUSDT", "ETHUSDT": "BTCUSDT"}

IS_TRAIN_START, IS_TRAIN_END = "2024-01-01", "2024-12-31"
IS_VAL_END = "2025-08-31"
OOS_END = "2026-07-01"

EXT_FACTOR_COLS = ["funding_rate_bps", "funding_z_3d", "funding_cum_3_bps", "xasset_corr_24", "xasset_relmom_24"]
HORIZONS = [1, 4, 12, 24]

# -freq base-frequency sweep, cross-asset factors only — see build_xasset_
# freq_sweep's docstring for why funding factors are excluded. window is the
# rolling-corr/momentum window in *bars*, scaled to cover ~24h/~48h wall-
# clock like the deployed xasset_corr_24/xasset_relmom_24 (24 bars@1h=24h;
# 6 bars@4h=24h; 2 bars@1d=48h — a rolling correlation is undefined at
# window=1). horizons are forward-period bar counts on the same scale.
BASE_TF_CONFIG = {
    "4h": {"window": 6, "horizons": [1, 2, 3, 6]},
    "1d": {"window": 2, "horizons": [1, 2, 3, 5]},
}

CANDIDATES = {
    "A: Funding Crowding Reversal (pure contrarian)": "funding_reversal",
    "B: Funding Reversal + Cross-Asset RelMom Confirm": "funding_relmom_confirm",
    "C: OHLCV-only baseline (no external data)": "ohlcv_baseline",
}


class _FundingCrowdingCandidate(BaseStrategy):
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


def _funding_symbol(symbol: str) -> str:
    """'BTCUSDT' -> 'BTC/USDT:USDT' — the ccxt perpetual symbol funding
    settles on (funding rate only exists for the perpetual contract, not the
    spot pair backtested here)."""
    if not symbol.endswith("USDT"):
        raise ValueError(f"unsupported symbol for funding-rate lookup: {symbol!r}")
    return f"{symbol[:-4]}/USDT:USDT"


def build_features(symbol: str, start: str, end: str, warmup_bars: int = 24 * 20) -> pd.DataFrame:
    """get_ohlcv (H1) -> attach_funding_features -> attach_cross_asset_features,
    for one asset. funding_rate/funding_cum_3 rescaled to bps — raw values
    are ~1e-4, too small to read comfortably next to price-derived factors."""
    raw = get_ohlcv(symbol, "1h", data_source="binance_spot", start=start, end=end, warmup_periods=warmup_bars)
    fetch_start = raw["timestamp"].min().strftime("%Y-%m-%d")
    df = attach_funding_features(raw, _funding_symbol(symbol), fetch_start, end)
    df = attach_cross_asset_features(df, REF_ASSET[symbol], "1h", "binance_spot", fetch_start, end, window=24)
    df["funding_rate_bps"] = df["funding_rate"] * 1e4
    df["funding_cum_3_bps"] = df["funding_cum_3"] * 1e4
    df = df.dropna(subset=EXT_FACTOR_COLS).reset_index(drop=True)
    return df.set_index("timestamp").rename_axis("ts")


def build_xasset_freq_sweep(symbol: str, base_tf: str, window: int, start: str, end: str) -> pd.DataFrame:
    """Minimal cross-asset factor set (xasset_corr/xasset_relmom only) at an
    alternate OHLCV base frequency, for the -freq sweep.

    Only the cross-asset factors are swept here — they're pure OHLCV-derived
    (rolling return correlation / relative momentum), so recomputing them at
    4H/1D is a legitimate "same computation, coarser bars" exercise. The
    funding factors are NOT included: funding settles on Binance's fixed
    00:00/08:00/16:00 UTC schedule (~3 prints/day) regardless of the OHLCV
    base timeframe used to bucket price bars — there is no "4H funding rate"
    or "1D funding rate" to recompute, only the same 3-per-day print series
    resampled onto coarser price bars, which doesn't test a different
    frequency hypothesis about the factor itself.
    """
    raw = get_ohlcv(symbol, base_tf, data_source="binance_spot", start=start, end=end, warmup_periods=window * 3)
    df = attach_cross_asset_features(raw, REF_ASSET[symbol], base_tf, "binance_spot", start, end, window=window)
    df = df.rename(columns={f"xasset_corr_{window}": "xasset_corr", f"xasset_relmom_{window}": "xasset_relmom"})
    return df.dropna(subset=["xasset_corr", "xasset_relmom"]).reset_index(drop=True)


def _screen_factors(df_train: pd.DataFrame, symbol: str) -> tuple[list, list]:
    """因子分析：外部因子多頻率橫掃（IS-Train，directional_hit_rate）."""
    pl_train = pl.from_pandas(
        df_train.reset_index()[["ts", "close"] + EXT_FACTOR_COLS].rename(columns={"ts": "date", "close": "price"})
    ).with_columns(pl.lit(symbol).alias("asset_id"))

    screen_results = fx.evaluate_horizons(
        pl_train, metrics={"dir_hit": directional_hit_rate()},
        factor_cols=EXT_FACTOR_COLS, forward_periods=HORIZONS, strict=False,
    )
    horizon_rows, significant = [], []
    print(f"{'Factor':<20} | {'Period':<6} | {'Hit Rate':<10} | {'PT Stat':<10} | {'p-value':<8}")
    print("-" * 66)
    for r in screen_results:
        m = r.metrics["dir_hit"]
        if not m.is_applicable:
            print(f"{r.factor:<20} | {r.forward_periods:<6} | not applicable ({m.metadata.get('reason')})")
            continue
        horizon_rows.append((r.factor, r.forward_periods, m.value, m.stat, m.p_value))
        mark = ""
        if m.p_value < 0.05 or m.p_value > 0.95:
            direction = "Trend" if m.p_value < 0.05 else "Reversion"
            significant.append((r.factor, r.forward_periods, m.value, m.p_value, direction))
            mark = "*"
        print(f"{r.factor:<20} | {r.forward_periods:<6} | {m.value:<10.4f} | {m.stat:<10.4f} | {m.p_value:<8.4f}{mark}")
    return horizon_rows, significant


def _best_horizon(horizon_rows: list, factor: str):
    rows = [r for r in horizon_rows if r[0] == factor]
    if not rows:
        return None
    return min(rows, key=lambda r: min(r[4], 1 - r[4]))


def main() -> None:
    print("=== 資產/資料層 ===")
    print(f"決策資產: {DECISION_ASSET} | 跨資產穩健性資產: {ROBUST_ASSET}")
    print("Loading BTCUSDT 1H OHLCV + funding + cross-asset(ETH) features...")
    full = build_features(DECISION_ASSET, IS_TRAIN_START, OOS_END)
    full = merge_daily_trend_gate(full)
    full = full.loc[IS_TRAIN_START:OOS_END]
    print(f"Total processed rows: {len(full)}")

    print("\n=== 樣本切分（IS-Train / IS-Val / OOS）===")
    splits = split_is_val_oos(full, IS_TRAIN_END, IS_VAL_END, OOS_END)
    for name, d in splits.items():
        print(f"  [{name:8s}] {len(d)} rows | {d.index.min().date()} - {d.index.max().date()}")

    print("\n=== 外部數據因子篩選（IS-Train，1H，factrix.evaluate_horizons） ===")
    horizon_rows, significant = _screen_factors(splits["IS-Train"], DECISION_ASSET)
    print("\nSignificant external-data factors on IS-Train (uncorrected):")
    for f, h, v, p, d in significant:
        print(f"  - {f} (h={h}h): HitRate={v:.4f}, p={p:.4f} ({d})")
    if not significant:
        print("  (none — external-data factors show no standalone directional edge on IS-Train)")

    screen_p_2sided = [min(2 * min(r[4], 1 - r[4]), 1.0) for r in horizon_rows]
    screen_p_holm = holm_adjusted_p(screen_p_2sided)
    screen_rows_adj = [(*row, p_adj) for row, p_adj in zip(horizon_rows, screen_p_holm)]
    ALPHA = 0.05
    screen_winners = [r for r in screen_rows_adj if r[5] < ALPHA]
    print(f"\nHolm-Bonferroni（factrix.stats.holm_adjusted_p, FWER, n={len(screen_p_2sided)}, alpha={ALPHA}）："
          f"{'有' if screen_winners else '沒有'}任何 (factor, horizon) 組合顯著")
    for r in screen_winners:
        print(f"  WINNER: {r[0]} @ {r[1]}h: hit={r[2]:.4f} p_raw={r[4]:.4f} p_holm={r[5]:.4f}")

    print("\n=== b 因子邊際穩定性（oos_decay，IS-Train 內部 70/30 切分） ===")
    decay_rows = []
    pl_train = pl.from_pandas(
        splits["IS-Train"].reset_index()[["ts", "close"] + EXT_FACTOR_COLS].rename(columns={"ts": "date", "close": "price"})
    ).with_columns(pl.lit(DECISION_ASSET).alias("asset_id"))
    for factor in EXT_FACTOR_COLS:
        best = _best_horizon(horizon_rows, factor)
        if best is None:
            decay_rows.append((factor, None, float("nan"), None, "not_applicable"))
            print(f"{factor}: skipped — not applicable at any horizon in ")
            continue
        h = best[1]
        data_h = fx.preprocess.compute_forward_return(pl_train, forward_periods=h)
        value_series = data_h.select(
            pl.col("date"), (pl.col(factor) * pl.col("forward_return")).alias("value"),
        ).drop_nulls().sort("date")
        decay = oos_decay(value_series)
        decay_rows.append((factor, h, decay.value, decay.metadata.get("sign_flipped"), decay.metadata.get("status")))
        print(f"{factor} @ {h}h: survival_ratio={decay.value:.4f} sign_flipped={decay.metadata.get('sign_flipped')} status={decay.metadata.get('status')}")

    print("\n=== -freq 基礎頻率橫掃（IS-Train，1H/4H/1D，僅 xasset_corr/xasset_relmom） ===")
    freq_sweep_rows = [("1h", f.replace("_24", ""), h, v, p) for f, h, v, s, p in horizon_rows if f in ("xasset_corr_24", "xasset_relmom_24")]
    for base_tf, cfg in BASE_TF_CONFIG.items():
        feat_tf = build_xasset_freq_sweep(DECISION_ASSET, base_tf, cfg["window"], IS_TRAIN_START, IS_TRAIN_END)
        pl_tf = pl.from_pandas(
            feat_tf[["timestamp", "close", "xasset_corr", "xasset_relmom"]].rename(columns={"timestamp": "date", "close": "price"})
        ).with_columns(pl.lit(DECISION_ASSET).alias("asset_id"))
        sweep_results = fx.evaluate_horizons(
            pl_tf, metrics={"dir_hit": directional_hit_rate()},
            factor_cols=["xasset_corr", "xasset_relmom"], forward_periods=cfg["horizons"], strict=False,
        )
        for r in sweep_results:
            m = r.metrics["dir_hit"]
            if not m.is_applicable:
                print(f"{base_tf:<8} | {r.factor:<14} | {r.forward_periods:>7} | not applicable ({m.metadata.get('reason')})")
                continue
            freq_sweep_rows.append((base_tf, r.factor, r.forward_periods, m.value, m.p_value))
            print(f"{base_tf:<8} | {r.factor:<14} | {r.forward_periods:>7} | {m.value:>8.4f} | {m.p_value:>8.4f}")

    freq_p_2sided = [min(2 * min(r[4], 1 - r[4]), 1.0) for r in freq_sweep_rows]
    freq_p_holm = holm_adjusted_p(freq_p_2sided) if freq_p_2sided else []
    freq_sweep_rows_adj = [(*row, p_adj) for row, p_adj in zip(freq_sweep_rows, freq_p_holm)]
    freq_sweep_winners = [r for r in freq_sweep_rows_adj if r[5] < ALPHA]
    print(f"\nHolm-Bonferroni（FWER, n={len(freq_p_2sided)}, two-sided, alpha={ALPHA}）："
          f"{'有' if freq_sweep_winners else '沒有'}任何 (base_tf, factor, horizon) 組合顯著")

    print("\n=== 頻率/持有期決定 ===")
    freq_note = (
        "換 4H/1D 重新取資料橫掃 xasset_corr/xasset_relmom 後（跨 base_tf×factor×horizon Holm 校正）"
        + ("，找到顯著組合，見-freq。" if freq_sweep_winners else "，三個基礎頻率上都沒有通過校正後的顯著性門檻。")
        + " funding 系列因子結算在固定的每日 3 次（00:00/08:00/16:00 UTC）排程上，跟 OHLCV 基礎頻率無關，"
          "沒有「4H/1D 版本的資金費率」可以重新計算，本節誠實排除、不假裝橫掃過。"
          "維持 1H 作為部署頻率，跟其他家族（trendpullback/mtf_trend_rsi）沿用同一組窗口，報告可直接互相比較。"
    )
    print(freq_note)

    print("\n=== 策略候選比較（IS-Val，含 real engine backtest） ===")
    val_results, best_label, best_sharpe = {}, None, -999.0
    for label, candidate in CANDIDATES.items():
        df = prepare_signals(splits["IS-Val"], candidate)
        stats = run_engine_backtest(df, _FundingCrowdingCandidate(), DECISION_ASSET)
        val_results[label] = stats
        print(f"  [{label}] {stats}")
        if stats["sharpe"] > best_sharpe:
            best_sharpe, best_label = stats["sharpe"], label
    winning_candidate = CANDIDATES[best_label]
    print(f"\nWinner on BTC IS-Val: {best_label} (Sharpe={best_sharpe:.4f})")

    print("\n=== b 盲測 OOS（BTC，同參數，全程未回頭調整）===")
    oos_df = prepare_signals(splits["OOS"], winning_candidate)
    oos_stats = run_engine_backtest(oos_df, _FundingCrowdingCandidate(), DECISION_ASSET)
    print(f"  {oos_stats}")

    print("\n=== c 事件層級因子顯著性（勝出候選的 entry_signal，Holm 校正） ===")
    entry_rows = []
    for split_name, split_df in splits.items():
        df = prepare_signals(split_df, winning_candidate)
        long_row = test_event_hit_rate(df, DECISION_ASSET, f"{split_name}/long", HORIZONS[-1], signal_col="long_entry")
        inverted = df.copy()
        inverted["close"] = -inverted["close"]
        short_row = test_event_hit_rate(inverted, DECISION_ASSET, f"{split_name}/short", HORIZONS[-1], signal_col="short_entry")
        entry_rows += [long_row, short_row]
        for row in (long_row, short_row):
            print(f"  {row['label']:20s} n_events={row['n_events']:5d}  hit_rate={row['hit_rate']:.4f}  p_raw={row['p_raw']:.4f}")
    print_holm_corrected(entry_rows)

    print("\n=== MAE/MFE 分布（勝出候選, IS-Train 進場事件） ===")
    is_train_df = prepare_signals(splits["IS-Train"], winning_candidate)
    train_stats = run_engine_backtest(is_train_df, _FundingCrowdingCandidate(), DECISION_ASSET, return_trades=True)
    mfe_mae_stats = mae_mfe_percentiles(train_stats["trade_list"], is_train_df)
    print(f"  {mfe_mae_stats}")

    print(f"\n=== 跨資產穩健性：{ROBUST_ASSET}，同參數不重調 ===")
    eth_full = build_features(ROBUST_ASSET, IS_TRAIN_START, OOS_END)
    eth_full = merge_daily_trend_gate(eth_full)
    eth_full = eth_full.loc[IS_TRAIN_START:OOS_END]
    eth_val_stats = run_engine_backtest(prepare_signals(eth_full.loc[IS_TRAIN_END:IS_VAL_END], winning_candidate), _FundingCrowdingCandidate(), ROBUST_ASSET)
    eth_oos_stats = run_engine_backtest(prepare_signals(eth_full.loc[IS_VAL_END:OOS_END], winning_candidate), _FundingCrowdingCandidate(), ROBUST_ASSET)
    print(f"  IS-Val: {eth_val_stats}")
    print(f"  OOS:    {eth_oos_stats}")

    print("\n=== b 跨資產核心因子穩健性（funding_z_3d / xasset_relmom_24，factrix） ===")
    core_factors = ["funding_z_3d", "xasset_relmom_24"]
    cross_asset_rows = []
    for symbol, feat in ((DECISION_ASSET, full), (ROBUST_ASSET, eth_full)):
        pl_feat = pl.from_pandas(
            feat.reset_index()[["ts", "close"] + core_factors].rename(columns={"ts": "date", "close": "price"})
        ).with_columns(pl.lit(symbol).alias("asset_id"))
        results = fx.evaluate_horizons(
            pl_feat, metrics={"dir_hit": directional_hit_rate()},
            factor_cols=core_factors, forward_periods=[24], strict=False,
        )
        for r in results:
            m = r.metrics["dir_hit"]
            if not m.is_applicable:
                print(f"  [{symbol}] {r.factor}: not applicable ({m.metadata.get('reason')})")
                continue
            cross_asset_rows.append((symbol, r.factor, m.value, m.p_value))
            print(f"  [{symbol}] {r.factor} @ 24h: hit_rate={m.value:.4f} p={m.p_value:.4f}")
    cross_p_2sided = [min(2 * min(p, 1 - p), 1.0) for _, _, _, p in cross_asset_rows]
    cross_p_holm = holm_adjusted_p(cross_p_2sided) if cross_p_2sided else []
    cross_asset_rows_adj = [(*row, p_adj) for row, p_adj in zip(cross_asset_rows, cross_p_holm)]

    # -------------------------------------------------------------
    # Report
    # -------------------------------------------------------------
    def fmt_screen_rows():
        if not significant:
            return "| （無顯著因子） | - | - | - | - |"
        return "\n".join(f"| {f} | {h}h | {v:.4f} | {p:.4f} | {d} |" for f, h, v, p, d in significant)

    def fmt_decay_rows():
        lines = []
        for f, h, v, flip, status in decay_rows:
            if h is None:
                lines.append(f"| {f} | - | - | - | {status} |")
            else:
                lines.append(f"| {f} | {h}h | {v:.4f} | {'是' if flip else '否'} | {status} |")
        return "\n".join(lines)

    def fmt_freq_sweep_rows():
        return "\n".join(f"| {tf} | {f} | {h} | {v:.4f} | {p:.4f} | {p_adj:.4f} |" for tf, f, h, v, p, p_adj in freq_sweep_rows_adj)

    def fmt_val_rows():
        return "\n".join(f"| {label} | {s['total_return']*100:.2f}% | {s['sharpe']:.4f} | {s['max_drawdown']*100:.2f}% | {s['trades']} |" for label, s in val_results.items())

    def fmt_entry_rows():
        return "\n".join(f"| {r['label']} | {r['n_events']} | {r['hit_rate']:.4f} | {r['p_raw']:.4f} |" for r in entry_rows)

    def fmt_cross_asset_rows():
        if not cross_asset_rows_adj:
            return "| （無可用結果） | - | - | - |"
        return "\n".join(f"| {a} | {f} | {v:.4f} | {p:.4f} | {p_adj:.4f} |" for a, f, v, p, p_adj in cross_asset_rows_adj)

    report = f"""# Funding-Rate Crowding Reversal — 研究報告

**時間**: 2026-07-21 | **決策資產**: {DECISION_ASSET} | **跨資產**: {ROBUST_ASSET}（同參數不重調）
**基礎頻率**: H1
**樣本切分**: IS-Train {IS_TRAIN_START}~{IS_TRAIN_END} | IS-Val ~{IS_VAL_END} | OOS ~{OOS_END}（跟
`strategies/experiments/trendpullback`/`mtf_trend_rsi` 用同一組窗口，三份報告可以直接互相比較）

流程依 `strategies/RESEARCH_METHODOLOGY.md` 的 ~順序執行，工具對應到本 repo 實際可用版本：
`strategies.module.data.ohlcv.get_ohlcv`（不是舊專案的 `utils/data.py`）、
`strategies.module.data.funding`/`cross_asset`（Binance 永續合約資金費率 + 跨資產滾動相關/動能，
公開 ccxt 端點，免驗證）、`librae.backtest.engine.Backtest`（透過 `run_engine_backtest`，本 repo 沒有
另外的手刻模擬器，也沒有沙箱 exec() 的網路限制——外部因子候選 A/B 一樣能拿到正式引擎數字，不像原始
研究受限於 `BacktestService` 的沙箱）。

## 資產/資料層

兩類外部數據：**永續合約資金費率**（籌碼/部位代理）與**跨資產連動性**（BTC vs ETH 滾動相關/相對動能）。
資金費率取自 `ccxt.binanceusdm`（公開、免驗證），歷史回溯到合約上市，涵蓋本次 IS-Train 起點
（{IS_TRAIN_START}）沒有問題。新增外部因子：`funding_rate_bps`（原始費率換算 bps）、`funding_z_3d`
（費率相對近 3 天自身分布的 z-score，衡量「擁擠程度」）、`funding_cum_3_bps`（近 3 次結算累積費率，
衡量「擁擠是否持續」）、`xasset_corr_24`（與 ETH 24 小時滾動報酬相關性）、`xasset_relmom_24`
（相對 ETH 的 24 小時超額動能）。

## 樣本切分

IS-Train（因子篩選）→ IS-Val（候選策略挑選）→ OOS（盲測，全程未回頭調整任何因子/候選/參數）。

## 外部數據因子篩選（IS-Train，`factrix.evaluate_horizons` + `directional_hit_rate`，1H）

| 因子 | 持有期 | Hit Rate | p-value | 效應方向 |
|------|-------|----------|---------|---------|
{fmt_screen_rows()}

多重檢定校正：`factrix.stats.holm_adjusted_p`（公開 API，factrix>=0.17 起可用，不手刻）。

## b 因子邊際穩定性（oos_decay，IS-Train 內部 70/30 切分）

| 因子 | 頻率 | 存活率 | 反號 | 狀態 |
|---|---|---|---|---|
{fmt_decay_rows()}

## -freq 基礎頻率橫掃（IS-Train，1H/4H/1D，僅 xasset_corr/xasset_relmom）

funding 系列因子結算排程跟 OHLCV 基礎頻率無關，未包含在此橫掃（見程式碼 `build_xasset_freq_sweep`
docstring）。

| 基礎頻率 | 因子 | Forward(bars) | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|---|
{fmt_freq_sweep_rows()}

## 頻率/持有期決定

{freq_note}

## 策略候選比較（IS-Val，`run_engine_backtest`，零成本）

| 候選 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|------|--------|--------|---------|---------|
{fmt_val_rows()}

**勝出**: {best_label}（Sharpe={best_sharpe:.4f}）

## b 盲測 OOS（BTC，同參數）

| 指標 | 數值 |
|------|------|
| 淨報酬 | {oos_stats['total_return']*100:.2f}% |
| Sharpe | {oos_stats['sharpe']:.4f} |
| 最大回撤 | {oos_stats['max_drawdown']*100:.2f}% |
| 交易次數 | {oos_stats['trades']} |

## c 事件層級因子顯著性（勝出候選 `{winning_candidate}` 的 entry_signal，event_hit_rate）

| 樣本/方向 | n_events | Hit Rate | p (raw) |
|---|---|---|---|
{fmt_entry_rows()}

Holm 校正（FWER，n={len([r for r in entry_rows if r['hit_rate']==r['hit_rate']])}，跨樣本×方向一起校正）結果見執行輸出（print_holm_corrected）。

## MAE/MFE 分布（勝出候選，IS-Train 進場事件）

{mfe_mae_stats}

## 跨資產穩健性（{ROBUST_ASSET}，同參數，不重調）

| 樣本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| IS-Val | {eth_val_stats['total_return']*100:.2f}% | {eth_val_stats['sharpe']:.4f} | {eth_val_stats['max_drawdown']*100:.2f}% | {eth_val_stats['trades']} |
| OOS | {eth_oos_stats['total_return']*100:.2f}% | {eth_oos_stats['sharpe']:.4f} | {eth_oos_stats['max_drawdown']*100:.2f}% | {eth_oos_stats['trades']} |

## b 跨資產核心因子穩健性（`funding_z_3d`/`xasset_relmom_24`，24h forward, factrix）

| 資產 | 因子 | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|
{fmt_cross_asset_rows()}

---

## 結論

（見程式執行輸出與上表；依 RESEARCH_METHODOLOGY 規則：若外部因子沒有通過校正後的顯著性門檻，且
候選 A/B 沒有在 IS-Val/OOS/跨資產上一致贏過候選 C，則本研究不建立 `strategy.py`，結論為「能取得、
能接進 factrix/real-engine 流程，但目前沒有觀察到它加值」。）
"""

    report_path = __file__.replace("factor_research.py", "report.md")
    with open(report_path, "w") as f_rep:
        f_rep.write(report)
    print(f"\nReport written to {report_path}")


if __name__ == "__main__":
    main()
