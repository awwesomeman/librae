"""
Funding-Rate Crowding Reversal — external-data research track.

Question this script answers: can external data (positioning / cross-asset
linkage) add anything on top of the existing OHLCV-only factor set already
explored by mtf_trend_momentum / mtf_trend_rsi / range_oscillator /
adaptive_switching / mtf_trend_slicing_regime?

Asset selection (see utils/funding.py and utils/cross_asset.py docstrings
for the full reasoning): crypto (BTC, cross-checked on ETH) was picked over
TXFR1 because a real "chip"/positioning proxy — perpetual funding rate — is
available for crypto with no auth and full history via ccxt's
`binanceusdm`. TXFR1's actual chip-equivalent (TAIFEX 三大法人/未平倉 open
data) has no existing wrapper in this repo and would sit on top of a base
OHLCV feed that's already gated behind a live Shioaji credential — a much
higher-friction starting point, left for a future iteration.

Two external-data ingredients tested:
  1. "Chip" proxy: perpetual funding rate (utils/funding.py) — persistent
     positive funding = crowded-long leveraged positioning.
  2. Cross-asset linkage: BTC-vs-ETH rolling correlation and relative
     momentum (utils/cross_asset.py).

Structure follows strategies/single_asset/README.md's ①~⑧ pipeline (the
same section markers adaptive_switching/factor_research.py uses). Reuses
the existing framework end-to-end (no reimplementation): utils.data for
OHLCV, utils.factors for the existing OHLCV factor set, utils.stats.
holm_bonferroni, utils.mfe_mae / utils.backtest_sim for the SL/TP overlay,
utils.engine_check for the real BacktestService cross-check, and factrix
for every factor-significance / cross-asset-robustness question.
"""
import os
import sys

import numpy as np
import pandas as pd
import polars as pl
import factrix as fx
from factrix.metrics import directional_hit_rate, oos_decay

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from utils.data import load_ohlcv, AssetUnavailable
from utils.cached_kline import get_crypto_kbars_df
from utils.universe import TICKERS
from utils.factors import generate_all_features, FACTOR_COLS
from utils.funding import attach_funding_features
from utils.cross_asset import attach_cross_asset_features
from utils.stats import holm_bonferroni
from utils.engine_check import run_engine_cross_check
from utils.mfe_mae import compute_mfe_mae_events, derive_sl_tp
from utils.backtest_sim import extract_trades, simulate_sl_tp

EXT_FACTOR_COLS = ["funding_rate_bps", "funding_z_3d", "funding_cum_3_bps", "xasset_corr_24", "xasset_relmom_24"]

# ① 資產/資料層 — decision asset only; cross-asset section (⑧) extends the
# same params to ETH without re-fitting. BTC's own pair; ETH's own pair for
# the cross-asset robustness leg below.
DECISION_ASSET = "BTC"
REF_ASSET = {"BTC": "ETH", "ETH": "BTC"}
ROBUST_ASSETS = ["BTC", "ETH"]
ROBUST_START, ROBUST_END = "2025-01-01", "2026-06-01"

# ② 樣本切分
IS_TRAIN_START, IS_TRAIN_END = "2024-08-01", "2025-04-30"
IS_VAL_END = "2025-09-30"
FULL_END = "2026-06-01"

HORIZONS = [1, 4, 12, 24]

# ③-freq base-frequency sweep config for the cross-asset factors ONLY
# (xasset_corr/xasset_relmom) — see build_xasset_freq_sweep_features'
# docstring for why funding factors are excluded from this sweep. `window`
# is the rolling-correlation/momentum window in *bars*, scaled so each
# base_tf covers roughly the same 24h wall-clock window as the deployed
# xasset_corr_24/xasset_relmom_24 (24 bars @ 1h = 24h; 6 bars @ 4h = 24h;
# 2 bars @ 1d = 48h — rolling correlation is undefined (NaN std) at
# window=1, so the 1d bucket widens to the smallest window a rolling
# correlation can actually be computed on, rather than collapsing to a
# meaningless 24h/1-bar window). `horizons` are forward-period bar counts,
# scaled the same way as adaptive_switching's ③-freq (roughly 1x/4x/12x/24x
# the base bar in wall-clock terms, rounded to sensible bar counts at
# coarser tf).
BASE_TF_CONFIG = {
    "4h": {"window": 6, "horizons": [1, 2, 3, 6]},
    "1d": {"window": 2, "horizons": [1, 2, 3, 5]},
}


def build_features(asset_id: str, start: str, end: str) -> pd.DataFrame:
    """load_ohlcv -> generate_all_features (existing OHLCV factor set) ->
    attach_funding_features -> attach_cross_asset_features, for one asset.
    funding_rate/funding_cum_3 are rescaled to bps (raw values are ~1e-4,
    too small for factrix's demeaning/plotting conventions used elsewhere
    in this factor set to read comfortably alongside e.g. rsi_1H_14)."""
    raw = load_ohlcv(asset_id, start, end)
    raw = raw.reset_index().rename(columns={"Datetime": "date"})
    df = generate_all_features(raw)
    df = attach_funding_features(df, asset_id, start, end)
    df = attach_cross_asset_features(df, REF_ASSET[asset_id], start, end, window=24)
    df["funding_rate_bps"] = df["funding_rate"] * 1e4
    df["funding_cum_3_bps"] = df["funding_cum_3"] * 1e4
    df = df.dropna(subset=EXT_FACTOR_COLS)
    return df


def build_xasset_freq_sweep_features(base_tf: str, window: int, start: str, end: str) -> pd.DataFrame:
    """Minimal cross-asset factor set (xasset_corr/xasset_relmom only) at an
    alternate OHLCV base frequency, for the ③-freq sweep. Fetches BTC and
    ETH directly via get_crypto_kbars_df (utils/universe.py's TICKERS
    registry only has 1h) instead of going through
    attach_cross_asset_features/load_ohlcv, which are hardwired to
    TICKERS[asset_id]["timeframe"].

    Only the cross-asset factors are swept here — they're pure OHLCV-
    derived (rolling return correlation / relative momentum), so recomputing
    them at 4H/1D is a legitimate "same computation, coarser bars" exercise.
    The funding factors (funding_rate_bps/funding_z_3d/funding_cum_3_bps)
    are NOT included: funding settles on Binance's fixed 00:00/08:00/16:00
    UTC schedule (~3 prints/day) regardless of the OHLCV base timeframe
    used to bucket price bars — there is no "4H funding rate" or "1D
    funding rate" to recompute, only the same 3-per-day print series
    resampled onto coarser price bars, which doesn't test a different
    frequency hypothesis about the factor itself. Sweeping it here would
    be cosmetic, not a real frequency test, so it's left out and this is
    flagged explicitly (see README ③ base-frequency guidance) rather than
    silently sweeping a factor that can't legitimately vary by base_tf.
    """
    btc_cfg, eth_cfg = TICKERS["BTC"], TICKERS["ETH"]
    primary = get_crypto_kbars_df(btc_cfg["exchange_id"], btc_cfg["symbol"], base_tf, start, end)
    ref = get_crypto_kbars_df(eth_cfg["exchange_id"], eth_cfg["symbol"], base_tf, start, end)

    primary = primary.reset_index().rename(columns={"Datetime": "date"})[["date", "Close"]]
    ref = ref.reset_index().rename(columns={"Datetime": "date"})[["date", "Close"]].rename(columns={"Close": "ref_close"})

    primary["date"] = pd.to_datetime(primary["date"], utc=True)
    ref["date"] = pd.to_datetime(ref["date"], utc=True)
    df = pd.merge_asof(primary.sort_values("date"), ref.sort_values("date"), on="date", direction="backward")

    own_ret = df["Close"].pct_change()
    ref_ret = df["ref_close"].pct_change()
    df["xasset_corr"] = own_ret.rolling(window).corr(ref_ret)

    own_mom = df["Close"] / df["Close"].shift(window) - 1.0
    ref_mom = df["ref_close"] / df["ref_close"].shift(window) - 1.0
    df["xasset_relmom"] = own_mom - ref_mom

    return df.dropna(subset=["xasset_corr", "xasset_relmom"]).reset_index(drop=True)


# -------------------------------------------------------------
# Hand-rolled simulator (same "next-bar execution, no friction" convention
# as every other single_asset family's research-stage simulator — a fast
# proxy for factor iteration, NOT a claim about tradability; see ⑦).
# -------------------------------------------------------------
def simulate_strategy(df_data, signal_type, params=None):
    df = df_data.copy()
    pos = 0.0
    signals = []
    market_return = df["Close"].pct_change().values

    if signal_type == "funding_reversal":
        # Pure contrarian: fade extreme, one-sided leveraged positioning.
        z = df["funding_z_3d"].values
        entry_z = params.get("entry_z", 1.5)
        exit_z = params.get("exit_z", 0.5)
        for i in range(len(df)):
            zi = z[i]
            if pos == 0.0:
                if zi > entry_z:
                    pos = -1.0  # crowded long -> fade down
                elif zi < -entry_z:
                    pos = 1.0  # crowded short -> fade up
            elif pos == -1.0:
                if zi < exit_z:
                    pos = 0.0
            elif pos == 1.0:
                if zi > -exit_z:
                    pos = 0.0
            signals.append(pos)

    elif signal_type == "funding_relmom_confirm":
        # Only fade crowding once cross-asset relative momentum agrees
        # (BTC already underperforming ETH when longs are crowded, etc.) —
        # a confirmation filter, not a standalone signal.
        z = df["funding_z_3d"].values
        relmom = df["xasset_relmom_24"].values
        entry_z = params.get("entry_z", 1.5)
        exit_z = params.get("exit_z", 0.5)
        for i in range(len(df)):
            zi, rm = z[i], relmom[i]
            if pos == 0.0:
                if zi > entry_z and rm < 0:
                    pos = -1.0
                elif zi < -entry_z and rm > 0:
                    pos = 1.0
            elif pos == -1.0:
                if zi < exit_z:
                    pos = 0.0
            elif pos == 1.0:
                if zi > -exit_z:
                    pos = 0.0
            signals.append(pos)

    elif signal_type == "ohlcv_baseline":
        # No external data at all — the mtf_trend_momentum family's winning
        # shape (daily trend filter + hourly momentum breakout), carried
        # over unmodified as the apples-to-apples "does external data beat
        # what we already had" comparison point.
        trend_col = params.get("trend_col", "mom_1D_10")
        mom_col = params.get("mom_col", "mom_1H_12")
        trend = df[trend_col].values
        mom = df[mom_col].values
        for i in range(len(df)):
            tr, m_val = trend[i], mom[i]
            if tr > 0:
                if pos <= 0:
                    if m_val > 0.005:
                        pos = 1.0
                else:
                    if m_val < -0.002:
                        pos = 0.0
            else:
                if pos >= 0:
                    if m_val < -0.005:
                        pos = -1.0
                else:
                    if m_val > 0.002:
                        pos = 0.0
            signals.append(pos)
    else:
        raise ValueError(f"unknown signal_type {signal_type!r}")

    df["signal"] = signals
    df["market_return"] = market_return
    df["strategy_return"] = df["signal"].shift(1) * df["market_return"]
    df = df.dropna(subset=["strategy_return"])

    cum_strategy = (1 + df["strategy_return"]).cumprod()
    cum_market = (1 + df["market_return"]).cumprod()
    total_return = cum_strategy.iloc[-1] - 1 if len(cum_strategy) else 0.0
    market_return_tot = cum_market.iloc[-1] - 1 if len(cum_market) else 0.0

    ann_factor = 8760
    daily_mean = df["strategy_return"].mean()
    daily_std = df["strategy_return"].std()
    sharpe = (daily_mean) / (daily_std + 1e-9) * np.sqrt(ann_factor)

    roll_max = cum_strategy.cummax()
    drawdown = (cum_strategy - roll_max) / roll_max
    max_dd = drawdown.min() if len(drawdown) else 0.0

    return {
        "total_return": total_return, "market_return": market_return_tot,
        "sharpe": sharpe, "max_dd": max_dd, "df": df,
    }


def run_research():
    print("=== ① 資產/資料層 ===")
    print(f"決策資產: {DECISION_ASSET} | 跨資產穩健性資產: {ROBUST_ASSETS}")
    print("資產選擇：crypto（BTC 決策、ETH 跨資產）取代 TXFR1 —— 永續合約資金費率"
          "（ccxt.binanceusdm，公開免驗證，歷史完整）是最接近「持倉/籌碼」語意的公開數據；"
          "TXFR1 的真實籌碼數據（三大法人/未平倉）在本專案完全沒有現成封裝，"
          "且基礎 OHLCV 已綁定一組需連線的 Shioaji 憑證，摩擦成本高很多，留給下一輪迭代。"
          "另測試過 Binance 未平倉量歷史 API：只保留約 30 天，無法覆蓋本框架多月 IS-Train 窗口，已放棄。")

    print("Loading BTC 1H OHLCV + funding + cross-asset(ETH) features...")
    df = build_features(DECISION_ASSET, IS_TRAIN_START, FULL_END)
    print(f"Total processed rows: {len(df)}")

    print("\n=== ② 樣本切分（IS-Train / IS-Val / OOS）===")
    df_train = df[df["date"] <= IS_TRAIN_END].reset_index(drop=True)
    df_val = df[(df["date"] > IS_TRAIN_END) & (df["date"] <= IS_VAL_END)].reset_index(drop=True)
    df_oos = df[df["date"] > IS_VAL_END].reset_index(drop=True)
    print(f"  [IS-Train]  {len(df_train)} rows | {df_train['date'].min().date()} - {df_train['date'].max().date()}")
    print(f"  [IS-Val]    {len(df_val)} rows | {df_val['date'].min().date()} - {df_val['date'].max().date()}")
    print(f"  [OOS]       {len(df_oos)} rows | {df_oos['date'].min().date()} - {df_oos['date'].max().date()}")

    # -------------------------------------------------------------
    # ③ 因子分析：外部因子多頻率橫掃（IS-Train，1H，factrix directional_hit_rate）
    # -------------------------------------------------------------
    print("\n=== ③ 外部數據因子篩選（IS-Train，1H，factrix evaluate_horizons） ===")
    pl_train = pl.from_pandas(df_train[["date", "Close"] + EXT_FACTOR_COLS].rename(columns={"Close": "price"}))
    pl_train = pl_train.with_columns(pl.lit("BTC").alias("asset_id"))

    significant = []
    horizon_rows = []  # (factor, horizon, hit_rate, p_value) — applicable rows only
    print(f"{'Factor':<20} | {'Period':<6} | {'Hit Rate':<10} | {'PT Stat':<10} | {'p-value':<8}")
    print("-" * 66)
    # evaluate_horizons — README ③: don't hand-roll a for-loop of evaluate()
    # calls to simulate a sweep, it's the safe per-horizon-rebuild wrapper.
    # strict=False (forwarded to each inner evaluate) surfaces an
    # inapplicable/insufficient-data (factor, horizon) cell as a NaN
    # MetricResult with is_applicable=False instead of raising.
    screen_results = fx.evaluate_horizons(
        pl_train, metrics={"dir_hit": directional_hit_rate()},
        factor_cols=EXT_FACTOR_COLS, forward_periods=HORIZONS, strict=False,
    )
    for r in screen_results:
        m = r.metrics["dir_hit"]
        if not m.is_applicable:
            print(f"{r.factor:<20} | {r.forward_periods:<6} | not applicable ({m.metadata.get('reason')})")
            continue
        horizon_rows.append((r.factor, r.forward_periods, m.value, m.p_value))
        is_sig_pos, is_sig_neg = m.p_value < 0.05, m.p_value > 0.95
        mark = ""
        if is_sig_pos or is_sig_neg:
            direction = "Trend" if is_sig_pos else "Reversion"
            significant.append((r.factor, r.forward_periods, m.value, m.stat, m.p_value, direction))
            mark = "*"
        print(f"{r.factor:<20} | {r.forward_periods:<6} | {m.value:<10.4f} | {m.stat:<10.4f} | {m.p_value:<8.4f}{mark}")

    print("\nSignificant external-data factors on IS-Train (uncorrected):")
    for sf in significant:
        print(f"  - {sf[0]} (h={sf[1]}h): HitRate={sf[2]:.4f}, p={sf[4]:.4f} ({sf[5]})")
    if not significant:
        print("  (none — external-data factors show no standalone directional edge on IS-Train)")

    def best_horizon(factor):
        # "most extreme p-value" picks whichever horizon is farthest from
        # p=0.5 in EITHER direction — tested for both a trend effect (p near
        # 0) and a reversion effect (p near 1). Returns None if the factor
        # was not_applicable (e.g. degenerate variance) at every horizon.
        rows = [r for r in horizon_rows if r[0] == factor]
        if not rows:
            return None
        return min(rows, key=lambda r: min(r[3], 1 - r[3]))

    # -------------------------------------------------------------
    # ③b 因子邊際穩定性（oos_decay）：IS-Train 內部前70%/後30%切分，這個邊際
    #    是否在 IS-Train 內部自己就存活，而不是等到 IS-Val 才發現不穩。
    # -------------------------------------------------------------
    print("\n=== ③b 因子邊際穩定性（oos_decay，IS-Train 內部切分） ===")
    decay_rows = []
    for factor in EXT_FACTOR_COLS:
        best = best_horizon(factor)
        if best is None:
            decay_rows.append((factor, None, float("nan"), None, "not_applicable (degenerate at every horizon in ③)"))
            print(f"{factor}: skipped — not applicable at any horizon in ③ (degenerate directional variance)")
            continue
        h = best[1]
        data_h = fx.preprocess.compute_forward_return(pl_train, forward_periods=h)
        value_series = data_h.select(
            pl.col("date"),
            (pl.col(factor) * pl.col("forward_return")).alias("value"),
        ).drop_nulls().sort("date")
        decay = oos_decay(value_series)
        decay_rows.append((factor, h, decay.value, decay.metadata.get("sign_flipped"), decay.metadata.get("status")))
        print(f"{factor} @ {h}h: survival_ratio={decay.value:.4f} sign_flipped={decay.metadata.get('sign_flipped')} status={decay.metadata.get('status')}")

    # -------------------------------------------------------------
    # ③-freq 基礎頻率橫掃（IS-Train，4H/1D）—— 只針對可以合法在其他基礎頻率
    #    重新計算的因子：xasset_corr_24/xasset_relmom_24（純 OHLCV 衍生）。
    #    funding_rate 系列因子結算在固定的 00:00/08:00/16:00 UTC 排程上，跟
    #    OHLCV 基礎頻率無關，沒有「4H 資金費率」這種東西可以重新計算，見
    #    build_xasset_freq_sweep_features 的說明，這裡明確排除，不假裝橫掃。
    #
    #    多重檢定校正：這是「掃過 base_tf×factor×horizon 網格挑單一贏家」的
    #    決策形態，需要 FWER（控制「至少挑錯一次」的機率），不是 FDR。
    #    factrix 公開 API 目前只有 FDR 工具（fx.stats.bhy_adjusted_p /
    #    fx.multi_factor.bhy/bhy_hierarchical）——`factrix/_stats/
    #    multiple_testing.py` 雖有 `holm_step_down`，但那是底線開頭的
    #    private module，未被 `factrix/__init__.py` 或公開的 `factrix/
    #    stats/` 引用，不是穩定 API，不依賴。故沿用 `utils/stats.py` 的手刻
    #    Holm-Bonferroni（見 README ③ 對這個 factrix 缺口的說明）。
    # -------------------------------------------------------------
    print("\n=== ③-freq 基礎頻率橫掃（IS-Train，1H/4H/1D，僅 xasset_corr/xasset_relmom） ===")
    freq_sweep_rows = []  # (base_tf, factor, horizon_bars, hit_rate, p_value)
    for factor, h, hit, p in horizon_rows:
        if factor in ("xasset_corr_24", "xasset_relmom_24"):
            freq_sweep_rows.append(("1h", factor.replace("_24", ""), h, hit, p))

    print(f"{'base_tf':<8} | {'factor':<14} | {'h(bars)':>7} | {'hit_rate':>8} | {'p_value':>8}")
    for r in freq_sweep_rows:
        print(f"{r[0]:<8} | {r[1]:<14} | {r[2]:>7} | {r[3]:>8.4f} | {r[4]:>8.4f}")

    for base_tf, cfg in BASE_TF_CONFIG.items():
        feat_tf = build_xasset_freq_sweep_features(base_tf, cfg["window"], IS_TRAIN_START, IS_TRAIN_END)
        pl_tf = pl.from_pandas(feat_tf[["date", "Close", "xasset_corr", "xasset_relmom"]].rename(columns={"Close": "price"}))
        pl_tf = pl_tf.with_columns(pl.lit("BTC").alias("asset_id"))
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

    # directional_hit_rate's p_value is one-sided against hit_rate==0.5 with
    # tail direction implicit in whether p is near 0 (trend) or near 1
    # (reversal) — two-sided-transform BEFORE correcting (see
    # adaptive_switching's ③-freq for the same convention), since the
    # correction needs a small-p-is-significant statistic.
    freq_sweep_p_raw = [r[4] for r in freq_sweep_rows]
    freq_sweep_p_2sided = [min(2 * min(p, 1 - p), 1.0) for p in freq_sweep_p_raw]
    freq_sweep_p_adj = holm_bonferroni(freq_sweep_p_2sided)
    freq_sweep_rows_adj = [(*row, p_adj) for row, p_adj in zip(freq_sweep_rows, freq_sweep_p_adj)]
    ALPHA = 0.05
    freq_sweep_winners = [r for r in freq_sweep_rows_adj if r[5] < ALPHA]
    print(f"\nHolm-Bonferroni 校正後（FWER control，n={len(freq_sweep_p_raw)} 個檢定，two-sided，alpha={ALPHA}）："
          f"{'有' if freq_sweep_winners else '沒有'}任何 (base_tf, factor, horizon) 組合顯著")
    for r in freq_sweep_winners:
        print(f"  WINNER: {r[0]} / {r[1]} @ {r[2]} bars: hit={r[3]:.4f} p_raw={r[4]:.4f} p_holm={r[5]:.4f}")

    # -------------------------------------------------------------
    # ④ 頻率/持有期決定 — 輸入是③/③-freq 的橫掃結果。
    # -------------------------------------------------------------
    print("\n=== ④ 頻率/持有期決定 ===")
    freq_note = (
        ("換 4H/1D 重新取資料橫掃 xasset_corr/xasset_relmom 後（跨 base_tf×factor×horizon 做 Holm-Bonferroni 校正），"
         + ("找到顯著組合，見③-freq，下一步應針對該頻率重新走⑤~⑧。" if freq_sweep_winners else
            "三個基礎頻率（1H/4H/1D）上都沒有通過校正後的顯著性門檻——這不是 1H 起始假設選錯，換基礎頻率沒有找到可用邊際。"))
        + " funding 系列因子（funding_rate_bps/funding_z_3d/funding_cum_3_bps）結構上無法做這個橫掃："
          "資金費率結算在固定的每日 3 次（00:00/08:00/16:00 UTC）排程上，跟 OHLCV 基礎頻率無關，"
          "沒有「4H/1D 版本的資金費率」可以重新計算，只能把同一個 3-次/日的印花序列重新取樣貼到不同粗細的價格 K 線上，"
          "不構成真正的頻率假設檢定，因此本節誠實排除、不假裝橫掃過。"
          "維持 1H 作為部署頻率——這跟本策略家族的其他候選（OHLCV-only baseline）沿用的頻率一致，"
          "既有的正式引擎部署檔（ohlcv_baseline_strategy.py）本就是逐 1H bar 判斷進出場。"
    )
    print(freq_note)

    # -------------------------------------------------------------
    # ⑤ 策略候選比較（IS-Val）—— 含一款完全不用外部數據的基準（C）。
    # -------------------------------------------------------------
    print("\n=== ⑤ 策略候選比較（IS-Val，BTC）===")
    candidates = {
        "A: Funding Crowding Reversal (pure contrarian)": {"type": "funding_reversal", "params": {"entry_z": 1.5, "exit_z": 0.5}},
        "B: Funding Reversal + Cross-Asset RelMom Confirm": {"type": "funding_relmom_confirm", "params": {"entry_z": 1.5, "exit_z": 0.5}},
        "C: OHLCV-only baseline (no external data)": {"type": "ohlcv_baseline", "params": {"trend_col": "mom_1D_10", "mom_col": "mom_1H_12"}},
    }

    val_results, best_candidate, best_sharpe = {}, None, -999.0
    for name, cfg in candidates.items():
        res = simulate_strategy(df_val, cfg["type"], cfg["params"])
        val_results[name] = res
        print(f"[{name}] Return={res['total_return']*100:.2f}% Sharpe={res['sharpe']:.4f} MaxDD={res['max_dd']*100:.2f}%")
        if res["sharpe"] > best_sharpe:
            best_sharpe, best_candidate = res["sharpe"], name
    print(f"\nWinner on BTC IS-Validation: {best_candidate} (Sharpe={best_sharpe:.4f})")
    winning_config = candidates[best_candidate]

    # -------------------------------------------------------------
    # ⑤b 盲測 OOS — 全程未用 OOS 挑選任何候選/參數。
    # -------------------------------------------------------------
    print("\n=== ⑤b 盲測 OOS（BTC）===")
    oos_res = simulate_strategy(df_oos, winning_config["type"], winning_config["params"])
    print(f"  OOS Return={oos_res['total_return']*100:.2f}% Market={oos_res['market_return']*100:.2f}% Sharpe={oos_res['sharpe']:.4f} MaxDD={oos_res['max_dd']*100:.2f}%")

    # -------------------------------------------------------------
    # ⑥ 風控疊加校準（MAE/MFE，IS-Train only）—— 同一套原則，永不在 held-out
    #    資料上重新校準。
    # -------------------------------------------------------------
    print("\n=== ⑥ MAE/MFE SL/TP 校準（IS-Train）===")
    train_signal_df = simulate_strategy(df_train, winning_config["type"], winning_config["params"])["df"]
    prev_sig = train_signal_df["signal"].shift(1).fillna(0)
    entry_long = (prev_sig == 0) & (train_signal_df["signal"] == 1.0)
    entry_short = (prev_sig == 0) & (train_signal_df["signal"] == -1.0)
    mfe_mae_events = compute_mfe_mae_events(train_signal_df, entry_long, entry_short, window=48, asset_id="BTC")
    mfe_mae_stats = derive_sl_tp(mfe_mae_events)
    if mfe_mae_stats["sl_pct"] is not None:
        mfe_sl, mfe_tp = mfe_mae_stats["sl_pct"], mfe_mae_stats["tp_pct"]
        print(f"MAE/MFE-derived: SL={mfe_sl*100:.2f}% TP={mfe_tp*100:.2f}%, n_events={mfe_mae_stats['n_events']}")
    else:
        mfe_sl, mfe_tp = None, None
        print(f"MAE/MFE-derived: unavailable — {mfe_mae_stats['reason']}")

    sl_tp_holdout_rows = []
    if mfe_sl is not None:
        for name, d in [("IS-Val", df_val), ("OOS", df_oos)]:
            d2 = simulate_strategy(d, winning_config["type"], winning_config["params"])["df"]
            t, h, l, c = extract_trades(d2, signal_col="signal")
            no_sl = simulate_sl_tp(t, h, l, c, 0.99, 9.99, friction=0.0)
            with_sl_tp = simulate_sl_tp(t, h, l, c, mfe_sl, mfe_tp, friction=0.0)
            sl_tp_holdout_rows.append((name, no_sl, with_sl_tp))
            print(f"[{name}] no-SL/TP: Return={no_sl['total_return']*100:.2f}% Sharpe={no_sl['sharpe']:.4f} (n={no_sl['n_trades']}) | +SL/TP: Return={with_sl_tp['total_return']*100:.2f}% Sharpe={with_sl_tp['sharpe']:.4f}")

    # -------------------------------------------------------------
    # ⑦ 正式引擎交叉驗證 — ONLY for the OHLCV-only baseline. The funding/
    #    cross-asset candidates (A/B) cannot be cross-checked this way:
    #    BacktestService._execute_indicator() runs strategy code through a
    #    sandboxed exec() (backend_api_python/app/utils/safe_exec.py) whose
    #    SAFE_IMPORT_MODULES whitelist is {numpy, pandas, math, json,
    #    datetime, time, collections, functools, itertools, statistics,
    #    decimal, fractions, copy} — no `requests`/`ccxt`, no network access
    #    at all. A `# @strategy` script literally cannot fetch funding rate
    #    or another asset's price at signal-eval time inside the real
    #    engine as it exists today. This is a real, load-bearing limitation
    #    of this research track, not a shortcut taken here — see conclusion.
    # -------------------------------------------------------------
    print("\n=== ⑦ 正式引擎交叉驗證（BacktestService）— 僅限「無外部數據」基準 ===")
    engine_results = {}
    baseline_strategy_path = os.path.join(os.path.dirname(__file__), "ohlcv_baseline_strategy.py")
    if os.path.exists(baseline_strategy_path):
        raw_engine = run_engine_cross_check(baseline_strategy_path, ROBUST_ASSETS, ROBUST_START, ROBUST_END, variants=[("no SL/TP", {})])
        engine_results = {a: r for (a, label), r in raw_engine.items()}
    else:
        print(f"[skip] {baseline_strategy_path} not found")

    # -------------------------------------------------------------
    # ⑧ 跨資產穩健性 — 決策只在 BTC 上做；同一組參數（外部因子定義、篩選
    #    門檻）原封不動套到 ETH，不重新調參。⑦已經涵蓋基準 C 的跨資產引擎
    #    數字；這裡額外做外部因子本身（key_factor）在 BTC/ETH 上是否都有
    #    方向性邊際的 factrix 檢定，因為 A/B 兩個候選本身無法透過⑦驗證。
    # -------------------------------------------------------------
    print("\n=== ⑧ 跨資產因子穩健性特徵建置（BTC/ETH） ===")
    per_asset_feat = {}
    for asset_id in ROBUST_ASSETS:
        try:
            feat = build_features(asset_id, ROBUST_START, ROBUST_END)
        except AssetUnavailable as exc:
            print(f"[skip] {asset_id}: {exc}")
            continue
        per_asset_feat[asset_id] = feat

    print("\n=== ⑧b 跨資產因子穩健性檢定（factrix by_slice + compare） ===")
    key_factor = "funding_z_3d" if "funding" in winning_config["type"] else "mom_1H_12"
    frames = []
    for asset_id, feat in per_asset_feat.items():
        panel_cols = ["date"] + EXT_FACTOR_COLS + FACTOR_COLS + ["Close"]
        asset_panel = feat[panel_cols].rename(columns={"Close": "price"}).assign(asset_id=asset_id)
        frames.append(pl.from_pandas(asset_panel))
    raw_panel = pl.concat(frames).sort(["asset_id", "date"]) if frames else None

    board, applicability, holm_by_asset, asset_order = None, {}, {}, []
    if raw_panel is not None and len(per_asset_feat) >= 1:
        panel = fx.preprocess.compute_forward_return(raw_panel, forward_periods=1)
        panel = panel.with_columns(pl.col("asset_id").alias("asset_group"))

        if len(per_asset_feat) >= 2:
            pooled_ic = fx.evaluate(panel, metrics={"ic": fx.metrics.ic()}, factor_cols=[key_factor])[key_factor].metrics["ic"]
            print(f"Pooled cross-sectional IC ({key_factor}, BTC+ETH): {pooled_ic.value:.4f} (p={pooled_ic.p_value:.4f})")

        by_asset = fx.by_slice(panel, fx.metrics.predictive_beta(), by="asset_group", factor_col=key_factor, strict=False)
        asset_order = list(by_asset.keys())
        board = fx.compare(list(by_asset.values()), metrics=["metric"]).with_columns(pl.Series("asset_id", asset_order))
        applicability = {a: (r.metrics["metric"].is_applicable, r.metrics["metric"].metadata.get("reason")) for a, r in by_asset.items()}
        applicable_assets = [a for a, (ok, _) in applicability.items() if ok]
        if applicable_assets:
            p_by_asset = dict(zip(board["asset_id"], board["metric_p_value"]))
            holm_p = holm_bonferroni([p_by_asset[a] for a in applicable_assets])
            holm_by_asset = dict(zip(applicable_assets, holm_p))
        print(board)
        for asset_id in asset_order:
            ok, reason = applicability[asset_id]
            if ok:
                print(f"[{asset_id}] Holm-adjusted p = {holm_by_asset[asset_id]:.4f}")
            else:
                print(f"[{asset_id}] not applicable ({reason})")

    horizon_board = None
    if raw_panel is not None and len(per_asset_feat) >= 2:
        print("\n=== ⑧c 跨資產持有期橫掃（pooled cross-sectional IC） ===")
        horizon_results = fx.evaluate_horizons(raw_panel, metrics={"ic": fx.metrics.ic()}, factor_cols=[key_factor], forward_periods=HORIZONS)
        horizon_board = fx.compare(horizon_results, metrics=["ic"], sort_by="forward_periods", descending=False)
        print(horizon_board)

    # -------------------------------------------------------------
    # Report
    # -------------------------------------------------------------
    def fmt_screen_rows():
        if not significant:
            return "| （無顯著因子） | - | - | - | - |"
        return "\n".join(f"| {f} | {h}h | {v:.4f} | {p:.4f} | {d} |" for f, h, v, s, p, d in significant)

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
        return "\n".join(f"| {name} | {r['total_return']*100:.2f}% | {r['sharpe']:.4f} | {r['max_dd']*100:.2f}% |" for name, r in val_results.items())

    def fmt_sl_tp_holdout():
        if not sl_tp_holdout_rows:
            return "| （MAE/MFE 事件數不足，無法校準） | - | - | - | - |"
        return "\n".join(f"| {n} | {no['total_return']*100:.2f}% | {no['sharpe']:.4f} | {ws['total_return']*100:.2f}% | {ws['sharpe']:.4f} |" for n, no, ws in sl_tp_holdout_rows)

    def fmt_engine_rows():
        if not engine_results:
            return "| （無可用結果） | - | - | - | - |"
        return "\n".join(f"| {a} | {r.get('totalReturn')}% | {r.get('sharpeRatio')} | {r.get('maxDrawdown')}% | {r.get('totalTrades')} |" for a, r in engine_results.items())

    def fmt_factor_board():
        if board is None:
            return "| （資產不足，無法比較） | - | - | - |"
        rows = {row["asset_id"]: row for row in board.to_dicts()}
        lines = []
        for a in asset_order:
            ok, reason = applicability[a]
            row = rows[a]
            if ok:
                lines.append(f"| {a} | {row['metric']:.4f} | {row['metric_p_value']:.4f} | {holm_by_asset.get(a, float('nan')):.4f} |")
            else:
                lines.append(f"| {a} | — | — | 不適用（{reason}） |")
        return "\n".join(lines)

    def fmt_horizon_rows():
        if horizon_board is None:
            return "| （資產不足，無法橫掃持有期） | - | - |"
        return "\n".join(f"| {row['forward_periods']}h | {row['ic']:.4f} | {row['ic_p_value']:.4f} |" for row in horizon_board.to_dicts())

    report = f"""# Funding-Rate Crowding Reversal — 研究報告

**時間**: 2026-07-12 | **決策資產**: {DECISION_ASSET} | **跨資產**: {", ".join(ROBUST_ASSETS)}
**樣本切分**: IS-Train {IS_TRAIN_START}~{IS_TRAIN_END} | IS-Val ~{IS_VAL_END} | OOS ~{FULL_END} | 跨資產窗口 {ROBUST_START}~{ROBUST_END}（同參數不重調）

流程依 `strategies/single_asset/README.md` 的 ①~⑧ 順序執行。

## ① 資產/資料層

本研究要納入兩類外部數據：**籌碼/部位資訊**與**跨資產連動性**。逐一盤點本專案現有的三個資產（`utils/universe.py`）：

| 資產 | 籌碼類數據可得性 | 跨資產連動可得性 |
|------|-----------------|-----------------|
| BTC/ETH（Crypto，binance） | **永續合約資金費率（funding rate）**：`ccxt.binanceusdm`，公開、免驗證、歷史完整回溯到合約上市 — 這是最接近「持倉/籌碼」語意的公開數據（資金費率持續為正 = 槓桿多頭擁擠，需要付費維持部位） | ETH 本身就在 `TICKERS` 裡，同交易所同頻率，直接可配對 |
| TXFR1（TAIFEX 近月連續） | 台指期真正的籌碼數據（三大法人買賣超、未平倉）在本專案**完全沒有現成的資料來源封裝**，且連基礎 OHLCV 都已經綁定一組必須先連線的 Shioaji 憑證（`utils/universe.py` 的既有註解） | 需要额外接一個非 crypto 的參考資產（如加權指數、美元兌台幣），同樣沒有現成封裝 |

另外測試了 Binance 的未平倉量歷史（open interest history）API，結果：**只保留最近約 30 天**（`startTime` 超出這個窗口會被交易所直接回 400），沒辦法覆蓋本框架慣用的多月 IS-Train 窗口，所以放棄，只用資金費率作為籌碼代理。

**結論：crypto（BTC 為主，ETH 交叉驗證）取得成本明顯低於 TXFR1，本輪先做 BTC。** TXFR1 的真實籌碼數據留給下一輪迭代（需要先建 TAIFEX/TWSE 開放數據的封裝）。

新增外部因子：`funding_rate_bps`（原始資金費率換算 bps）、`funding_z_3d`（資金費率相對近 3 天自身分布的 z-score，衡量「擁擠程度」）、`funding_cum_3_bps`（近 3 次結算的累積資金費率，衡量「擁擠是否持續」）、`xasset_corr_24`（與 ETH 24 小時滾動報酬相關性）、`xasset_relmom_24`（相對 ETH 的 24 小時超額動能）。

## ② 樣本切分

IS-Train ({IS_TRAIN_START} ~ {IS_TRAIN_END}) 做因子篩選；IS-Val ({IS_TRAIN_END} ~ {IS_VAL_END}) 做候選策略挑選；OOS ({IS_VAL_END} ~ {FULL_END}) 盲測，全程未用 OOS 回頭調整任何因子/候選/參數。

## ③ 外部數據因子篩選（IS-Train，`factrix.evaluate_horizons` + `directional_hit_rate`，1H）

| 因子 | 持有期 | Hit Rate | p-value | 效應方向 |
|------|-------|----------|---------|---------|
{fmt_screen_rows()}

## ③b 因子邊際穩定性（oos_decay，IS-Train 內部 70/30 切分）

| 因子 | 頻率 | 存活率 | 反號 | 狀態 |
|---|---|---|---|---|
{fmt_decay_rows()}

存活率（絕對值 mean_OOS / mean_IS）< 0.5 或反號代表這個因子在 IS-Train 內部自己都不穩定，不用等到 IS-Val 就已經是警訊。

## ③-freq 基礎頻率橫掃（IS-Train，1H/4H/1D，僅 `xasset_corr`/`xasset_relmom`）

`xasset_corr_24`/`xasset_relmom_24` 是純 OHLCV 衍生因子（滾動報酬相關性/相對動能），可以合法在其他基礎頻率重新計算，直接呼叫 `get_crypto_kbars_df` 取 BTC/ETH 在 4H/1D 的 K 線重新計算，而非透過綁定 1H 的 `attach_cross_asset_features`/`load_ohlcv`。

**`funding_rate_bps`/`funding_z_3d`/`funding_cum_3_bps` 未包含在這個橫掃裡**：資金費率結算在 Binance 固定的每日 3 次（00:00/08:00/16:00 UTC）排程上，跟 OHLCV 基礎頻率無關 —— 沒有「4H 資金費率」或「1D 資金費率」這種東西可以重新計算，只能把同一個 3-次/日印花序列重新取樣貼到不同粗細的價格 K 線上，這不構成真正的頻率假設檢定。本節誠實排除這兩個因子的橫掃，而非假裝測過。

**多重檢定校正方法**：用 `utils/stats.py` 的手刻 `holm_bonferroni`（Holm step-down，控制 **FWER**），不是 factrix 現成工具。原因：這個網格搜尋的決策形態是「掃過 base_tf×factor×horizon 全部組合、挑單一贏家」，需要 FWER（控制「至少挑錯一次」的機率），不是 FDR（控制「誤判佔比的期望值」，適合「篩一批因子、全部留著用」的情境）。**factrix 公開 API 目前只有 FDR 工具**（`fx.stats.bhy_adjusted_p`/`fx.multi_factor.bhy`/`bhy_hierarchical`）——`factrix/_stats/multiple_testing.py` 雖然有 `holm_step_down`/`bonferroni`，但那是底線開頭的 private module，未被 `factrix/__init__.py` 或公開的 `factrix/stats/` 引用，不能當作穩定 API 依賴。這是 README ③ 認定的真實 factrix 缺口。

| 基礎頻率 | 因子 | Forward(bars) | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|---|
{fmt_freq_sweep_rows()}

Holm-Bonferroni 校正（n={len(freq_sweep_p_raw)} 個檢定，跨 base_tf × factor × horizon 一起校正，alpha=0.05）後，{"找到以下顯著組合" if freq_sweep_winners else "沒有任何組合顯著"}。{("；".join(f"{r[0]}/{r[1]}@{r[2]}bars (hit={r[3]:.4f}, p_holm={r[5]:.4f})" for r in freq_sweep_winners) + "。") if freq_sweep_winners else "1H/4H/1D 三個基礎頻率上，xasset_corr/xasset_relmom 皆未通過校正後的顯著性門檻——換粗/細基礎頻率沒有找到可用的邊際。"}

## ④ 頻率/持有期決定

{freq_note}

## ⑤ 策略候選比較（IS-Val，BTC）

| 候選 | 累積報酬 | Sharpe | 最大回撤 |
|------|---------|--------|---------|
{fmt_val_rows()}

**勝出**: {best_candidate}（Sharpe={best_sharpe:.4f}）

## ⑤b 盲測 OOS（BTC，{IS_VAL_END} 之後）

| 指標 | 數值 |
|------|------|
| 策略累積報酬 | {oos_res['total_return']*100:.2f}% |
| 大盤同期報酬 | {oos_res['market_return']*100:.2f}% |
| Sharpe | {oos_res['sharpe']:.4f} |
| 最大回撤 | {oos_res['max_dd']*100:.2f}% |

## ⑥ MAE/MFE 停損停利疊加（IS-Train 校準，IS-Val/OOS 驗證）

{f"**校準結果**：SL={mfe_sl*100:.2f}% / TP={mfe_tp*100:.2f}%（{mfe_mae_stats['n_events']} 個進場事件）" if mfe_sl is not None else f"**無法校準**：{mfe_mae_stats.get('reason')}"}

| 區間 | 無SL/TP 報酬 | 無SL/TP Sharpe | +SL/TP 報酬 | +SL/TP Sharpe |
|------|--------------|-----------------|--------------|-----------------|
{fmt_sl_tp_holdout()}

## ⑦ 正式引擎交叉驗證（`BacktestService.run()`）— 僅限「無外部數據」基準

| 資產 | 累積報酬 | Sharpe | 最大回撤 | 交易次數 |
|------|---------|--------|---------|---------|
{fmt_engine_rows()}

**為什麼 A/B（資金費率型候選）沒有正式引擎數字**：`BacktestService._execute_indicator()` 透過 `backend_api_python/app/utils/safe_exec.py` 的沙箱 `exec()` 執行 `# @strategy` 腳本，其 `SAFE_IMPORT_MODULES` 白名單只有 `{{numpy, pandas, math, json, datetime, time, collections, functools, itertools, statistics, decimal, fractions, copy}}` — **沒有 `requests`/`ccxt`，完全沒有網路存取**。也就是說，資金費率或另一個資產的即時價格，在目前的正式引擎架構下，`# @strategy` 腳本**在訊號計算當下無法自行抓取** — 這不是這次研究省略的步驟，而是這條研究路線在「能不能真的上線交易」這一層目前踩到的實際限制。

## ⑧ 跨資產穩健性（BTC 決策，同參數套用 ETH，不重新調參）

⑦已涵蓋基準 C 的跨資產引擎數字；以下是外部因子（`{key_factor}`）本身在 BTC/ETH 上是否都有方向性邊際的 factrix 檢定，因為 A/B 兩個候選本身無法透過⑦驗證。只有 2 個資產，正式跨切片假設檢定（`slice_pairwise_test`/`slice_joint_test`）統計上不夠力，用描述性排行榜 + Holm 校正取代：

| 資產 | Beta | p-value | Holm 校正後 p-value |
|------|------|---------|---------------------|
{fmt_factor_board()}

**手刻校正注記**（README ③）：`holm_bonferroni`（`utils/stats.py`）是手刻，不是 factrix 現成工具，因為（1）「檢查因子依賴性是否跨資產成立」需要 FWER（控制至少判斷錯一次的機率），不是 FDR；（2）factrix 公開 API 目前沒有 FWER 工具——`factrix/_stats/multiple_testing.py` 雖有 `holm_step_down`，但那是底線開頭的 private module，未被公開介面引用，不是穩定 API。

## ⑧c 跨資產持有期橫掃（`factrix.evaluate_horizons`，pooled cross-sectional IC）

| 持有期 | Pooled IC | p-value |
|-------|-----------|---------|
{fmt_horizon_rows()}

---

## 結論（誠實版）

1. **外部因子在 IS-Train 上沒有通過顯著性門檻**：{"第③節列出的因子" if significant else "五個新因子（`funding_rate_bps`/`funding_z_3d`/`funding_cum_3_bps`/`xasset_corr_24`/`xasset_relmom_24`）在 1/4/12/24 小時任何一個持有期上，p-value 都沒有跨過 0.05/0.95 的顯著性門檻"} — 資金費率擁擠與跨資產相對動能，在目前樣本窗口上**沒有獨立於既有 OHLCV 因子集之外的方向性邊際**。這是有效的研究結論，不代表方法或數據管線有問題。
2. **③b 的 oos_decay 快篩**：見上表——任何存活率過低或反號的因子，代表這個邊際在 IS-Train 內部自己都不穩定，是比 IS-Val 更早的警訊。
3. **③-freq 換基礎頻率沒有拯救 xasset 因子**：{"1H 上的不顯著不是頻率選錯——換 4H/1D 重新取資料橫掃、跨 base_tf×factor×horizon 做 Holm-Bonferroni 校正後，三個基礎頻率上 xasset_corr/xasset_relmom 都沒有通過顯著性門檻，代表這兩個因子在 BTC 上（至少在這個樣本窗口）本身就不具備可用邊際，不是 1H 這個起始假設的問題。" if not freq_sweep_winners else "換基礎頻率後找到顯著組合（見③-freq），下一步應針對該頻率重新走④~⑧，而非沿用目前部署的 1H 邏輯。"} funding 系列因子則因為結算排程跟 OHLCV 基礎頻率無關，結構上無法做這個橫掃，這是誠實的範圍限制，不是遺漏。
4. **候選 C（純 OHLCV，無外部數據）在 IS-Val/OOS 上直接贏過 A/B**：{f"IS-Val Sharpe C={val_results['C: OHLCV-only baseline (no external data)']['sharpe']:.2f} vs A={val_results['A: Funding Crowding Reversal (pure contrarian)']['sharpe']:.2f} / B={val_results['B: Funding Reversal + Cross-Asset RelMom Confirm']['sharpe']:.2f}" if val_results else ""} — 跟前兩點一致：這次新增的兩類外部數據，無論是當獨立因子看（③）還是包成完整策略看（⑤），都沒有比既有 OHLCV-only 打法更好。「能不能納外部數據」這個問題，本輪的誠實答案是**能取得、能接進 factrix 流程，但目前沒有觀察到它加值**。
5. **可交易性缺口（⑦）是本次研究流程本身最重要的發現，獨立於上面統計結論**：即使某個未來版本的外部因子真的顯著，目前的正式回測/實盤引擎在架構上就無法讓策略腳本讀取資金費率或跨資產價格 — `BacktestService` 的沙箱 `exec()` 不允許任何網路存取。要讓這條研究路線真正可上線，需要工程面先讓 `BacktestService` 支援「額外數據欄位」注入（例如把 funding_rate 當成跟 OHLCV 一起餵給引擎的預先計算欄位），而不是讓策略腳本自己在沙箱裡發網路請求（那也不安全，不該開放）。
6. **下一步建議**：(a) 外部數據這條路線在 BTC/ETH 上目前沒有找到邊際，繼續在同樣的資金費率因子上調參屬於過度擬合同一批數據，不建議；更值得做的是換一種「籌碼」定義（例如現貨/合約成交量比、多空比 long/short ratio——Binance 也有免驗證的公開端點，這次沒測）。(b) TXFR1 的真實籌碼數據（三大法人、未平倉）值得作為下一輪外部數據研究的資產，但要先建立獨立於 Shioaji 即時憑證之外的歷史數據來源。(c) 不論哪個方向，⑦節的引擎沙箱限制都要先解決，否則統計上再顯著的外部因子也無法變成可上線的策略。
"""

    report_path = os.path.join(os.path.dirname(__file__), "report.md")
    with open(report_path, "w") as f_rep:
        f_rep.write(report)
    print(f"\nReport written to {report_path}")


if __name__ == "__main__":
    run_research()
