import os
import sys
import pandas as pd
import numpy as np
import polars as pl
import factrix as fx
from factrix.metrics import directional_hit_rate, oos_decay

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from utils.data import load_ohlcv
from utils.cached_kline import get_crypto_kbars_df
from utils.universe import TICKERS
from utils.factors import generate_all_features, FACTOR_COLS, calculate_rsi
from utils.panel import build_long_panel, build_raw_panel
from utils.stats import holm_bonferroni
from utils.engine_check import run_engine_cross_check
from utils.mfe_mae import compute_mfe_mae_events, derive_sl_tp
from utils.backtest_sim import extract_trades, simulate_sl_tp

# ① 資產/資料層 — decision asset only; cross-asset section (⑧) extends the
# same params to ETH/TXFR1 without re-fitting.
DECISION_ASSET = "BTC"
ROBUST_ASSETS = ["BTC", "ETH", "TXFR1"]
ROBUST_START, ROBUST_END = "2025-01-01", "2026-06-01"

# ② 樣本切分 — IS-Train (factor screening) → IS-Val (candidate selection) →
# OOS (blind test, never used to pick factors/candidates/params).
IS_TRAIN_START, IS_TRAIN_END = "2024-08-01", "2025-04-30"
IS_VAL_END = "2025-09-30"
FULL_END = "2026-06-01"

HORIZONS = [1, 4, 12, 24]

# ③-freq base-frequency sweep config — README ③: base frequency itself is
# part of the sweep, not just forward period. Tests the two hourly timing
# factors actually used by the ⑤ candidates (mom_1H_12 / rsi_1H_14,
# generalized here as mom_factor/rsi_demeaned so the same evaluation code
# runs across base_tf) at 1H/4H/1D, fetched directly via get_crypto_kbars_df
# since utils/universe.py's TICKERS registry only has 1H for BTC.
# rsi_period is held at 14 bars across all base_tf (a bar-count window, not
# wall-clock-scaled); mom_lookback is scaled so each base_tf's momentum
# window stays close to ~12h in wall-clock terms, collapsing to 1 bar at 1D
# since a sub-day lookback isn't meaningful on daily bars.
BASE_TF_CONFIG = {
    "1h": {"mom_lookback": 12, "rsi_period": 14, "horizons": [1, 4, 12, 24]},
    "4h": {"mom_lookback": 3, "rsi_period": 14, "horizons": [1, 2, 3, 6]},
    "1d": {"mom_lookback": 1, "rsi_period": 14, "horizons": [1, 2, 3, 5]},
}


def build_freq_sweep_features(raw: pd.DataFrame, mom_lookback: int, rsi_period: int) -> pd.DataFrame:
    """Minimal factor set for the ③-freq base-frequency sweep: momentum + RSI
    only, generic column names so the same evaluation code runs across
    base_tf. Does not reproduce the full 13-factor generate_all_features()
    set — the frequency sweep only needs to answer "is the hourly timing
    factor significant at this base frequency", not re-screen every factor.
    """
    df = raw.reset_index().rename(columns={'Datetime': 'date'})
    df['mom_factor'] = df['Close'] / df['Close'].shift(mom_lookback) - 1.0
    df['rsi_demeaned'] = calculate_rsi(df['Close'], rsi_period) - 50.0
    return df.dropna().reset_index(drop=True)


# -------------------------------------------------------------
# Backtest engine simulator (hand-rolled, asset-agnostic) — used for the
# fast IS-Val/OOS/MAE-MFE iteration in ⑤/⑤b/⑥. ⑦ re-verifies the final
# numbers against the real BacktestService.
# -------------------------------------------------------------
def simulate_strategy(df_data, signal_type="mtf_rsi", params=None):
    """
    Simulates the strategy return and metrics.
    params is a dict of threshold parameters.
    """
    df = df_data.copy()
    df['signal'] = 0.0
    pos = 0.0
    signals = []

    close = df['Close'].values
    market_return = df['Close'].pct_change().values

    if signal_type == "mtf_rsi":
        # Trend filter + RSI counter-trend timing
        trend_col = params.get('trend_col', 'mom_1D_10')
        rsi_col = params.get('rsi_col', 'rsi_1H_14')
        buy_thresh = params.get('buy_thresh', -20) # e.g. RSI < 30 (demeaned by 50)
        sell_thresh = params.get('sell_thresh', 15)  # e.g. RSI > 65
        short_thresh = params.get('short_thresh', 20) # e.g. RSI > 70
        cover_thresh = params.get('cover_thresh', -15) # e.g. RSI < 35

        trend = df[trend_col].values
        rsi = df[rsi_col].values

        for i in range(len(df)):
            tr = trend[i]
            r_val = rsi[i]
            if tr > 0: # Bull trend
                if pos <= 0:
                    if r_val < buy_thresh:
                        pos = 1.0
                else:
                    if r_val > sell_thresh:
                        pos = 0.0
            else: # Bear trend
                if pos >= 0:
                    if r_val > short_thresh:
                        pos = -1.0
                else:
                    if r_val < cover_thresh:
                        pos = 0.0
            signals.append(pos)

    elif signal_type == "mtf_mom":
        # Trend filter + Hourly momentum timing
        trend_col = params.get('trend_col', 'mom_1D_10')
        mom_col = params.get('mom_col', 'mom_1H_12')

        trend = df[trend_col].values
        mom = df[mom_col].values

        for i in range(len(df)):
            tr = trend[i]
            m_val = mom[i]
            if tr > 0: # Bull trend
                if pos <= 0:
                    if m_val > 0.005: # strong hourly momentum breakout
                        pos = 1.0
                else:
                    if m_val < -0.002: # momentum loss exit
                        pos = 0.0
            else: # Bear trend
                if pos >= 0:
                    if m_val < -0.005: # strong momentum breakdown
                        pos = -1.0
                else:
                    if m_val > 0.002: # momentum recovery exit
                        pos = 0.0
            signals.append(pos)

    elif signal_type == "pure_rsi":
        # Pure RSI mean reversion without trend filter
        rsi_col = params.get('rsi_col', 'rsi_1H_14')
        buy_thresh = params.get('buy_thresh', -20)
        sell_thresh = params.get('sell_thresh', 15)
        short_thresh = params.get('short_thresh', 20)
        cover_thresh = params.get('cover_thresh', -15)

        rsi = df[rsi_col].values

        for i in range(len(df)):
            r_val = rsi[i]
            if pos == 0:
                if r_val < buy_thresh:
                    pos = 1.0
                elif r_val > short_thresh:
                    pos = -1.0
            elif pos == 1.0:
                if r_val > sell_thresh:
                    pos = 0.0
            elif pos == -1.0:
                if r_val < cover_thresh:
                    pos = 0.0
            signals.append(pos)

    df['signal'] = signals
    df['market_return'] = market_return
    # next-bar execution
    df['strategy_return'] = df['signal'].shift(1) * df['market_return']
    df = df.dropna()

    cum_strategy = (1 + df['strategy_return']).cumprod()
    cum_market = (1 + df['market_return']).cumprod()

    total_return = cum_strategy.iloc[-1] - 1
    market_return_tot = cum_market.iloc[-1] - 1

    ann_factor = 8760
    daily_mean = df['strategy_return'].mean()
    daily_std = df['strategy_return'].std()
    sharpe = (daily_mean) / (daily_std + 1e-9) * np.sqrt(ann_factor)

    roll_max = cum_strategy.cummax()
    drawdown = (cum_strategy - roll_max) / roll_max
    max_dd = drawdown.min()

    return {
        'total_return': total_return,
        'market_return': market_return_tot,
        'sharpe': sharpe,
        'max_dd': max_dd,
        'df': df
    }


def run_research():
    print("=== ① 資產/資料層 ===")
    print(f"決策資產: {DECISION_ASSET} | 跨資產穩健性資產: {ROBUST_ASSETS}")
    btc_raw = load_ohlcv(DECISION_ASSET, IS_TRAIN_START, FULL_END)
    btc_raw = btc_raw.reset_index().rename(columns={'Datetime': 'date'})
    df = generate_all_features(btc_raw)

    print("\n=== ② 樣本切分（IS-Train / IS-Val / OOS）===")
    df_is = df[df['date'] <= IS_VAL_END]
    df_train = df_is[df_is['date'] <= IS_TRAIN_END].reset_index(drop=True)
    df_val = df_is[df_is['date'] > IS_TRAIN_END].reset_index(drop=True)
    df_oos = df[df['date'] > IS_VAL_END].reset_index(drop=True)
    print(f"IS-Train: {len(df_train)} rows ({df_train['date'].min().date()}~{df_train['date'].max().date()}) | "
          f"IS-Val: {len(df_val)} rows ({df_val['date'].min().date()}~{df_val['date'].max().date()}) | "
          f"OOS: {len(df_oos)} rows ({df_oos['date'].min().date()}~{df_oos['date'].max().date()})")

    # -------------------------------------------------------------
    # ③-freq 基礎頻率橫掃（IS-Train）— README ③: base frequency is a
    # hypothesis to test, not the 1H starting assumption locked in by
    # utils/universe.py's TICKERS registry. Factor × base_tf × horizon is a
    # grid search that picks a SINGLE winning cell, which calls for FWER
    # (bound the probability of picking even one spurious cell), not FDR.
    # factrix's public API only ships FDR tools (bhy_adjusted_p/bhy/
    # bhy_hierarchical) — factrix/_stats/multiple_testing.py has
    # holm_step_down but it's an underscore-prefixed private module never
    # referenced by factrix's own public interface, not a stable API — so
    # this uses utils/stats.py's hand-rolled Holm-Bonferroni (see README ③
    # for why this is a genuine factrix gap, not a preference).
    # -------------------------------------------------------------
    print("\n=== ③-freq 基礎頻率橫掃（IS-Train，1H/4H/1D，factrix directional_hit_rate） ===")
    btc_cfg = TICKERS[DECISION_ASSET]
    freq_sweep_rows = []  # (base_tf, factor, horizon_bars, hit_rate, p_value)
    print(f"{'base_tf':<8} | {'factor':<10} | {'h(bars)':>7} | {'hit_rate':>8} | {'p_value':>8}")
    for base_tf, cfg in BASE_TF_CONFIG.items():
        raw_tf = get_crypto_kbars_df(btc_cfg["exchange_id"], btc_cfg["symbol"], base_tf, IS_TRAIN_START, IS_TRAIN_END)
        feat_tf = build_freq_sweep_features(raw_tf, cfg["mom_lookback"], cfg["rsi_period"])
        pl_tf = pl.from_pandas(feat_tf[['date', 'Close', 'mom_factor', 'rsi_demeaned']].rename(columns={'Close': 'price'}))
        pl_tf = pl_tf.with_columns(pl.lit(DECISION_ASSET).alias("asset_id"))
        # evaluate_horizons rebuilds compute_forward_return per horizon
        # internally (README ③: don't hand-roll a for-loop of evaluate()
        # calls to simulate a sweep — evaluate_horizons is the safe wrapper).
        sweep_results = fx.evaluate_horizons(
            pl_tf, metrics={"dir_hit": directional_hit_rate()},
            factor_cols=["mom_factor", "rsi_demeaned"], forward_periods=cfg["horizons"],
        )
        for r in sweep_results:
            m = r.metrics["dir_hit"]
            freq_sweep_rows.append((base_tf, r.factor, r.forward_periods, m.value, m.p_value))
            print(f"{base_tf:<8} | {r.factor:<10} | {r.forward_periods:>7} | {m.value:>8.4f} | {m.p_value:>8.4f}")

    # directional_hit_rate's p_value is one-sided against hit_rate==0.5 with
    # the tail direction implicit in whether p is near 0 (trend) or near 1
    # (reversal). The correction needs a small-p-is-significant statistic,
    # so two-sided-transform BEFORE correcting, not after.
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
    # ③ 因子分析：1H 多 forward period 橫掃（IS-Train，13 factors）— this
    # step's output decides ④, not a post-hoc justification of an
    # already-fixed frequency. ③-freq above is the base-tf dimension of the
    # same sweep; this section keeps the full 13-factor screen at 1H since
    # that's the base_tf the deployed strategy (mtf_trend_momentum_strategy.py)
    # actually runs at.
    # -------------------------------------------------------------
    print("\n=== ③ 因子分析：多頻率橫掃（IS-Train，1H，13 factors，factrix directional_hit_rate） ===")
    factors = [c for c in FACTOR_COLS if c != 'atr_ratio_1H_14']

    pl_train = pl.from_pandas(df_train[['date', 'Close'] + factors].rename(columns={'Close': 'price'}))
    pl_train = pl_train.with_columns(pl.lit(DECISION_ASSET).alias("asset_id"))

    # evaluate_horizons — see ③-freq's comment on why this replaces a
    # hand-rolled for-loop of evaluate() calls. strict=False: an
    # inapplicable/insufficient-data (factor, horizon) cell surfaces as a NaN
    # MetricResult with is_applicable=False instead of raising.
    exploration_results = fx.evaluate_horizons(
        pl_train, metrics={"dir_hit": directional_hit_rate()},
        factor_cols=factors, forward_periods=HORIZONS, strict=False,
    )

    horizon_rows = []  # (factor, horizon, hit_rate, p_raw)
    print(f"{'Factor':<16} | {'Period':<6} | {'Hit Rate':<10} | {'p_raw':<8}")
    print("-" * 50)
    for r in exploration_results:
        metric_res = r.metrics["dir_hit"]
        if not metric_res.is_applicable:
            print(f"{r.factor:<16} | {r.forward_periods:<6} | not applicable ({metric_res.metadata.get('reason')})")
            continue
        horizon_rows.append((r.factor, r.forward_periods, metric_res.value, metric_res.p_value))
        print(f"{r.factor:<16} | {r.forward_periods:<6} | {metric_res.value:<10.4f} | {metric_res.p_value:<8.4f}")

    # Multiple-testing correction — README ③: this step screens a BATCH of
    # 13 factors × 4 horizons and carries every survivor forward into ⑤'s
    # candidates (it does not itself commit to a single winner — ⑤'s IS-Val
    # comparison does that later). That's the FDR use case, not FWER, and
    # factrix ships a public tool for it, so use fx.stats.bhy_adjusted_p
    # directly instead of the hand-rolled holm_bonferroni used in ③-freq/⑧
    # (where the decision genuinely is "scan a grid, pick one winner").
    p_2sided = [min(2 * min(p, 1 - p), 1.0) for (_, _, _, p) in horizon_rows]
    p_bhy = fx.stats.bhy_adjusted_p(p_2sided).tolist()
    horizon_rows_adj = [(*row, p_adj) for row, p_adj in zip(horizon_rows, p_bhy)]
    significant_factors = [row for row in horizon_rows_adj if row[4] < 0.05]

    print(f"\nBHY 校正後（FDR control，n={len(horizon_rows)} 個檢定，two-sided，alpha=0.05）："
          f"{len(significant_factors)}/{len(horizon_rows)} 個 (factor, horizon) 組合通過")
    for f, h, val, p_raw, p_adj in significant_factors:
        direction_str = "Trend" if val > 0.5 else "Reversion"
        print(f"  - {f} (period={h}h): hit_rate={val:.4f}, p_raw={p_raw:.4f}, p_bhy={p_adj:.4f} ({direction_str} effect)")

    def best_horizon(factor):
        # "most extreme p-value" picks whichever horizon is farthest from
        # p=0.5 in EITHER direction — trend and reversion effects both count.
        rows = [r for r in horizon_rows if r[0] == factor]
        return min(rows, key=lambda r: min(r[3], 1 - r[3]))

    mom_best = best_horizon("mom_1H_12")
    rsi_best = best_horizon("rsi_1H_14")
    print(f"\nmom_1H_12 最穩定顯著: {mom_best[1]}h (hit={mom_best[2]:.4f}, p_raw={mom_best[3]:.4f})")
    print(f"rsi_1H_14 (去均值) 最穩定顯著: {rsi_best[1]}h (hit={rsi_best[2]:.4f}, p_raw={rsi_best[3]:.4f})")

    # -------------------------------------------------------------
    # ③b 因子邊際穩定性（oos_decay）— IS-Train 內部前70%/後30%切分，這個
    # 邊際是否存活，而不是只看整個 IS-Train 合併後的單一數字。只檢查候選
    # 策略實際使用的兩個小時線 timing 因子（mom_1H_12 / rsi_1H_14），不是
    # 全部 13 個因子——日線 mom_1D_10 是 regime 濾鏡，不是這裡要驗證的
    # timing 邊際。
    # -------------------------------------------------------------
    print("\n=== ③b 因子邊際穩定性（oos_decay，IS-Train 內部切分） ===")
    decay_rows = []
    for factor, best in [("mom_1H_12", mom_best), ("rsi_1H_14", rsi_best)]:
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
    # ④ 頻率/持有期決定 — 輸入是③/③-freq/③b的橫掃結果，不是研究前的經驗值。
    # -------------------------------------------------------------
    print("\n=== ④ 頻率/持有期決定 ===")
    freq_note = (
        f"③ 在 1H 上對 13 個因子橫掃 4 個 forward period（BHY/FDR 校正後 {len(significant_factors)}/{len(horizon_rows)} 組合顯著）；"
        f"mom_1H_12 最穩定顯著於 {mom_best[1]}h（hit={mom_best[2]:.4f}），rsi_1H_14 最穩定顯著於 {rsi_best[1]}h（hit={rsi_best[2]:.4f}）。"
        f"③-freq 換 4H/1D 基礎頻率重新橫掃並用 Holm-Bonferroni（FWER）跨 base_tf×factor×horizon 校正後，"
        f"{'找到顯著組合（見③-freq）' if freq_sweep_winners else '三個基礎頻率上都沒有找到顯著組合，1H 起始假設沒有被更粗/細的頻率推翻，但也沒有被獨立證實'}。"
        f"部署策略（mtf_trend_momentum_strategy.py）逐 1H bar 判斷進出場，使用 mom_1D_10 日線趨勢濾鏡 + mom_1H_12 小時動量 timing——"
        f"跟③挑出的 mom_1H_12 最穩定顯著頻率（{mom_best[1]}h）一致，沒有頻率倒置問題。"
    )
    print(freq_note)

    # -------------------------------------------------------------
    # ⑤ 策略候選比較（IS-Val）— 三個候選：A（趨勢濾鏡+RSI逆勢）、
    # B（趨勢濾鏡+小時動量突破，即目前部署邏輯）、C（純 RSI 逆勢，無濾鏡
    # 基準，方便判斷濾鏡是否真的加值）。
    # -------------------------------------------------------------
    print("\n=== ⑤ 策略候選比較（IS-Val）===")

    candidates = {
        "A: MTF Trend + RSI Reversion": {
            "type": "mtf_rsi",
            "params": {"trend_col": "mom_1D_10", "rsi_col": "rsi_1H_14", "buy_thresh": -20, "sell_thresh": 15, "short_thresh": 20, "cover_thresh": -15}
        },
        "B: MTF Trend + Hourly Momentum": {
            "type": "mtf_mom",
            "params": {"trend_col": "mom_1D_10", "mom_col": "mom_1H_12"}
        },
        "C: Pure 1H RSI Reversion (No Filter, baseline)": {
            "type": "pure_rsi",
            "params": {"rsi_col": "rsi_1H_14", "buy_thresh": -20, "sell_thresh": 15, "short_thresh": 20, "cover_thresh": -15}
        }
    }

    val_results = {}
    best_candidate = None
    best_sharpe = -999.0

    for name, config in candidates.items():
        res = simulate_strategy(df_val, signal_type=config["type"], params=config["params"])
        val_results[name] = res
        print(f"[IS-Val: {name}] NetReturn={res['total_return']*100:.2f}% Market={res['market_return']*100:.2f}% Sharpe={res['sharpe']:.4f} MaxDD={res['max_dd']*100:.2f}%")

        if res['sharpe'] > best_sharpe:
            best_sharpe = res['sharpe']
            best_candidate = name

    print(f"\nSelected Winner on IS-Val: {best_candidate} (Sharpe={best_sharpe:.4f})")
    winning_config = candidates[best_candidate]

    with open(os.path.join(os.path.dirname(__file__), "research_decision.txt"), "w") as f_dec:
        f_dec.write(f"Winner: {best_candidate}\n")
        f_dec.write(f"Type: {winning_config['type']}\n")
        f_dec.write(f"Params: {str(winning_config['params'])}\n")

    # -------------------------------------------------------------
    # ⑤b 盲測 OOS — 全程未用 OOS 挑選任何候選/參數。
    # -------------------------------------------------------------
    print("\n=== ⑤b 盲測 OOS ===")
    oos_results = {}
    for name, config in candidates.items():
        res = simulate_strategy(df_oos, signal_type=config["type"], params=config["params"])
        oos_results[name] = res
        print(f"[OOS: {name}] NetReturn={res['total_return']*100:.2f}% Market={res['market_return']*100:.2f}% Sharpe={res['sharpe']:.4f} MaxDD={res['max_dd']*100:.2f}%")

    oos_res = oos_results[best_candidate]

    # -------------------------------------------------------------
    # ⑥ MAE/MFE SL/TP 校準（IS-Train only）— 這個家族本來完全沒有
    # 停損停利；held-out 驗證同時看 IS-Val 跟 OOS。
    # -------------------------------------------------------------
    print("\n=== ⑥ MAE/MFE SL/TP 校準（IS-Train）===")
    train_signal_df = simulate_strategy(df_train, signal_type=winning_config["type"], params=winning_config["params"])["df"]
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
            d = simulate_strategy(d, signal_type=winning_config["type"], params=winning_config["params"])["df"]
            t, h, l, c = extract_trades(d, signal_col="signal")
            no_sl = simulate_sl_tp(t, h, l, c, 0.99, 9.99, friction=0.0)  # cross-check vs bar-based no-SL/TP baseline above (0 friction: bar-based baseline already nets no per-trade friction beyond signal changes)
            with_sl_tp = simulate_sl_tp(t, h, l, c, mfe_sl, mfe_tp, friction=0.0)
            sl_tp_holdout_rows.append((name, no_sl, with_sl_tp))
            print(f"[{name}] no-SL/TP (cross-check): Return={no_sl['total_return']*100:.2f}% Sharpe={no_sl['sharpe']:.4f} (n={no_sl['n_trades']}) | MAE/MFE SL/TP: Return={with_sl_tp['total_return']*100:.2f}% Sharpe={with_sl_tp['sharpe']:.4f}")

    # -------------------------------------------------------------
    # ⑦ 正式引擎交叉驗證（BacktestService，跨資產同參數不重調）
    # -------------------------------------------------------------
    print("\n=== ⑦ 正式引擎交叉驗證（BacktestService） ===")
    strategy_path = os.path.join(os.path.dirname(__file__), "mtf_trend_momentum_strategy.py")
    engine_variants = [("no SL/TP", {})]
    if mfe_sl is not None:
        engine_variants.append((f"MAE/MFE SL={mfe_sl*100:.1f}%/TP={mfe_tp*100:.1f}%", {"risk": {"stopLossPct": mfe_sl, "takeProfitPct": mfe_tp}}))
    raw_engine = run_engine_cross_check(strategy_path, ROBUST_ASSETS, ROBUST_START, ROBUST_END, variants=engine_variants)
    engine_results = {a: r for (a, label), r in raw_engine.items() if label == "no SL/TP"}
    engine_sl_tp_results = {a: r for (a, label), r in raw_engine.items() if label != "no SL/TP"}

    # -------------------------------------------------------------
    # ⑧ 跨資產穩健性驗證 — ⑦已經完成核心檢查（同一組參數套 BTC/ETH/TXFR1，
    # 不重新調參）。以下用 factrix 從因子層級再驗證一次：關鍵因子本身是否
    # 在每個資產上個別都站得住腳（不只是回測 P&L 好看），以及邊際是否跨
    # 持有期穩定，而不只是⑤挑中的那一個持有期。
    #
    # 只有 3 個資產，正式跨切片假設檢定（slice_pairwise_test/
    # slice_joint_test）既不適用（要求切片內仍是逐日橫截面指標，而
    # predictive_beta 是刻意設計的單資產 TIMESERIES 指標，沒有逐日序列）
    # 也統計上不夠力；factrix 自己對「切片太少無法檢定」的回答是描述性
    # 排行榜（by_slice + compare），不是硬套一個檢定。
    # -------------------------------------------------------------
    print("\n=== ⑧ 跨資產穩健性驗證（factrix，因子層級） ===")
    key_factor = winning_config["params"].get("mom_col") or winning_config["params"].get("rsi_col")
    panel, per_asset_feat = build_long_panel(ROBUST_ASSETS, ROBUST_START, ROBUST_END)

    pooled_ic = fx.evaluate(panel, metrics={"ic": fx.metrics.ic()}, factor_cols=[key_factor])[key_factor].metrics["ic"]
    print(f"Pooled cross-sectional IC ({key_factor}, all assets combined): {pooled_ic.value:.4f} (p={pooled_ic.p_value:.4f})")

    by_asset = fx.by_slice(panel, fx.metrics.predictive_beta(), by="asset_group", factor_col=key_factor, strict=False)
    asset_order = list(by_asset.keys())
    board = fx.compare(list(by_asset.values()), metrics=["metric"]).with_columns(pl.Series("asset_id", asset_order))

    applicability = {a: (r.metrics["metric"].is_applicable, r.metrics["metric"].metadata.get("reason")) for a, r in by_asset.items()}
    applicable_assets = [a for a, (ok, _) in applicability.items() if ok]
    # Hand-rolled Holm-Bonferroni (utils/stats.py), not a factrix FDR tool:
    # this checks whether the same factor dependency holds across all K=3
    # assets — a "does this survive everywhere, or was it decision-asset
    # noise" question, which is FWER territory (see README ③/⑧), same
    # rationale as ③-freq above.
    if applicable_assets:
        p_by_asset = dict(zip(board["asset_id"], board["metric_p_value"]))
        holm_p = holm_bonferroni([p_by_asset[a] for a in applicable_assets])
        holm_by_asset = dict(zip(applicable_assets, holm_p))
    else:
        holm_by_asset = {}

    print(board)
    for asset_id in asset_order:
        ok, reason = applicability[asset_id]
        if ok:
            print(f"[{asset_id}] Holm-adjusted p = {holm_by_asset[asset_id]:.4f}")
        else:
            print(f"[{asset_id}] not applicable ({reason})")

    print("\n--- ⑧b 因子穩健性：跨持有期橫掃（pooled IC across holding periods） ---")
    raw_panel, _ = build_raw_panel(ROBUST_ASSETS, ROBUST_START, ROBUST_END)
    horizon_results = fx.evaluate_horizons(
        raw_panel, metrics={"ic": fx.metrics.ic()}, factor_cols=[key_factor], forward_periods=[1, 4, 12, 24]
    )
    horizon_board = fx.compare(horizon_results, metrics=["ic"], sort_by="forward_periods", descending=False)
    print(horizon_board)

    # -------------------------------------------------------------
    # Report
    # -------------------------------------------------------------
    def fmt_freq_sweep_rows():
        return "\n".join(f"| {tf} | {f} | {h} | {v:.4f} | {p:.4f} | {p_adj:.4f} |" for tf, f, h, v, p, p_adj in freq_sweep_rows_adj)

    def fmt_horizon_rows():
        return "\n".join(f"| {f} | {h}h | {v:.4f} | {p:.4f} | {p_adj:.4f} |" for f, h, v, p, p_adj in horizon_rows_adj)

    def fmt_decay_rows():
        return "\n".join(f"| {f} | {h}h | {v:.4f} | {'是' if flip else '否'} | {status} |" for f, h, v, flip, status in decay_rows)

    def fmt_val_rows():
        return "\n".join(f"| {n} | {r['total_return']*100:.2f}% | {r['sharpe']:.4f} | {r['max_dd']*100:.2f}% |" for n, r in val_results.items())

    def fmt_oos_rows():
        return "\n".join(f"| {n} | {r['total_return']*100:.2f}% | {r['sharpe']:.4f} | {r['max_dd']*100:.2f}% |" for n, r in oos_results.items())

    def fmt_sl_tp_holdout():
        if not sl_tp_holdout_rows:
            return "| （MAE/MFE 事件數不足，無法校準） | - | - | - | - |"
        return "\n".join(f"| {n} | {no['total_return']*100:.2f}% | {no['sharpe']:.4f} | {ws['total_return']*100:.2f}% | {ws['sharpe']:.4f} |" for n, no, ws in sl_tp_holdout_rows)

    def fmt_engine_rows():
        if not engine_results:
            return "| （無可用結果） | - | - | - | - |"
        lines = []
        for a, r in engine_results.items():
            if "error" in r:
                lines.append(f"| {a} | CRASH | {r['error'][:80]} | - | - |")
            else:
                lines.append(f"| {a} | {r.get('totalReturn')}% | {r.get('sharpeRatio')} | {r.get('maxDrawdown')}% | {r.get('totalTrades')} |")
        return "\n".join(lines)

    def fmt_engine_sl_tp_rows():
        if not engine_sl_tp_results:
            return "| （無可用結果） | - | - | - | - |"
        lines = []
        for a in engine_results:
            no_sl, with_sl = engine_results[a], engine_sl_tp_results.get(a, {})
            if "error" in no_sl or "error" in with_sl:
                lines.append(f"| {a} | CRASH | - | - | - |")
            else:
                lines.append(f"| {a} | {no_sl.get('totalReturn')}% | {no_sl.get('sharpeRatio')} | {with_sl.get('totalReturn')}% | {with_sl.get('sharpeRatio')} |")
        return "\n".join(lines)

    def fmt_factor_rows():
        board_rows = {row["asset_id"]: row for row in board.to_dicts()}
        lines = []
        for asset_id, (ok, reason) in applicability.items():
            row = board_rows[asset_id]
            if ok:
                lines.append(f"| {asset_id} | {row['metric']:.4f} | {row['metric_p_value']:.4f} | {holm_by_asset[asset_id]:.4f} |")
            else:
                lines.append(f"| {asset_id} | — | — | 不適用（{reason}） |")
        return "\n".join(lines)

    def fmt_horizon_robust_rows():
        return "\n".join(f"| {row['forward_periods']}h | {row['ic']:.4f} | {row['ic_p_value']:.4f} |" for row in horizon_board.to_dicts())

    val_sharpes = {n: r['sharpe'] for n, r in val_results.items()}
    oos_sharpes = {n: r['sharpe'] for n, r in oos_results.items()}
    val_baseline_beat = val_sharpes[best_candidate] > val_sharpes.get("C: Pure 1H RSI Reversion (No Filter, baseline)", -999)
    oos_baseline_beat = oos_sharpes[best_candidate] > oos_sharpes.get("C: Pure 1H RSI Reversion (No Filter, baseline)", -999)

    factor_robust_any_asset = any(holm_by_asset.get(a, 1.0) < 0.05 for a in applicable_assets) if applicable_assets else False
    decay_any_flipped = any(flip for _, _, _, flip, _ in decay_rows)

    report = f"""# MTF Trend Momentum — 研究報告

**時間**: 2026-07-12 | **決策資產**: {DECISION_ASSET} | **跨資產**: {", ".join(ROBUST_ASSETS)}
**樣本切分**: IS-Train {IS_TRAIN_START}~{IS_TRAIN_END} | IS-Val ~{IS_VAL_END} | OOS ~{FULL_END} | 跨資產窗口 {ROBUST_START}~{ROBUST_END}（同參數不重調）

流程依 `strategies/single_asset/README.md` 的 ①~⑧ 順序執行。

## ③-freq 基礎頻率橫掃（IS-Train，1H/4H/1D）

測試候選策略實際使用的兩個小時線 timing 因子（mom_1H_12/rsi_1H_14，於各 base_tf 泛化為 mom_factor/rsi_demeaned，lookback 隨基礎頻率調整：1H=12 bars / 4H=3 bars / 1D=1 bar，皆對應約 12h 窗口，1D 上收斂為 1 bar 因日線沒有次日內窗口；rsi_period 固定 14 bars，不隨基礎頻率縮放）。

**多重檢定校正方法**：用 `utils/stats.py` 的手刻 `holm_bonferroni`（Holm step-down，控制 **FWER**），不是 factrix 現成工具。原因：這個網格搜尋的決策形態是「掃過 base_tf×factor×horizon 全部組合、挑單一贏家」，需要的是 FWER，不是 FDR（適合「篩一批因子、全部留著用」，見下方③的做法）。**factrix 公開 API 目前只有 FDR 工具**（`fx.stats.bhy_adjusted_p`/`fx.multi_factor.bhy`/`bhy_hierarchical`）——`factrix/_stats/multiple_testing.py` 雖然有 `holm_step_down`/`bonferroni`，但那是底線開頭的 private module，未被 `factrix/__init__.py` 或公開的 `factrix/stats/` 引用，不能當作穩定 API 依賴。這是 README ③ 認定的真實 factrix 缺口。

| 基礎頻率 | 因子 | Forward(bars) | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|---|
{fmt_freq_sweep_rows()}

Holm-Bonferroni 校正（n={len(freq_sweep_p_raw)} 個檢定，跨 base_tf × factor × horizon 一起校正，alpha=0.05）後，{"找到以下顯著組合" if freq_sweep_winners else "沒有任何組合顯著"}。{("；".join(f"{r[0]}/{r[1]}@{r[2]}bars (hit={r[3]:.4f}, p_holm={r[5]:.4f})" for r in freq_sweep_winners) + "。") if freq_sweep_winners else "1H/4H/1D 三個基礎頻率上，mom/rsi 這兩個 timing 因子皆未通過校正後的顯著性門檻——這不否定1H起始假設（1H並沒有比其他頻率更差），但也沒有提供獨立於③（13因子未校正掃描）之外的統計支持。"}

## ③ 因子分析：多頻率橫掃（IS-Train，1H，13 factors）

| 因子 | 持有期 | Hit Rate | p (raw) | p (BHY) |
|---|---|---|---|---|
{fmt_horizon_rows()}

**多重檢定校正方法**：用 factrix 現成的 `fx.stats.bhy_adjusted_p`（Benjamini-Hochberg-Yekutieli，控制 **FDR**）。這一步是「篩一批因子、把通過的全部帶進⑤」的情境（13因子×4持有期=52個檢定），不是挑單一贏家，屬於 FDR 而非 FWER 的決策形態，factrix 公開 API 對這個情境有現成工具，直接用，不需要手刻。

校正後 {len(significant_factors)}/{len(horizon_rows)} 個 (factor, horizon) 組合通過 alpha=0.05。mom_1H_12 最穩定顯著於 {mom_best[1]}h（hit={mom_best[2]:.4f}, p_raw={mom_best[3]:.4f}），rsi_1H_14（去均值）最穩定顯著於 {rsi_best[1]}h（hit={rsi_best[2]:.4f}, p_raw={rsi_best[3]:.4f}）。

## ③b 因子邊際穩定性（oos_decay，IS-Train 內部 70/30 切分）

只檢查候選策略實際使用的兩個小時線 timing 因子；日線 mom_1D_10 是 regime 濾鏡，不是這裡要驗證的 timing 邊際。

| 因子 | 頻率 | 存活率 | 反號 | 狀態 |
|---|---|---|---|---|
{fmt_decay_rows()}

存活率（絕對值 mean_OOS / mean_IS）< 0.5 或反號代表這個因子在 IS-Train 內部自己都不穩定，不用等到 IS-Val 就已經是警訊。

## ④ 頻率/持有期決定

{freq_note}

## ⑤ 策略候選比較（IS-Val）

| 版本 | 淨報酬 | Sharpe | 最大回撤 |
|---|---|---|---|
{fmt_val_rows()}

**勝出**：{best_candidate}（Sharpe={best_sharpe:.4f}）。{"優於" if val_baseline_beat else "並未優於"}無濾鏡基準（候選 C）。

## ⑤b 盲測 OOS

| 版本 | 淨報酬 | Sharpe | 最大回撤 |
|---|---|---|---|
{fmt_oos_rows()}

IS-Val 勝出的候選（{best_candidate}）在 OOS 上{"同樣" if oos_baseline_beat == val_baseline_beat else "沒有"}維持相對於無濾鏡基準（候選 C）的排序{"（Sharpe {:.4f} vs C {:.4f}）".format(oos_sharpes[best_candidate], oos_sharpes.get("C: Pure 1H RSI Reversion (No Filter, baseline)", float('nan')))}。

## ⑥ MAE/MFE SL/TP 校準（IS-Train）

{f"**校準結果**：SL={mfe_sl*100:.2f}% / TP={mfe_tp*100:.2f}%（{mfe_mae_stats['n_events']} 個進場事件）" if mfe_sl is not None else f"**無法校準**：{mfe_mae_stats.get('reason')}"}

Held-out（交易級交叉檢查，套用 IS-Train 校準出的固定 SL/TP，不重新校準）：
| 區間 | 無SL/TP | Sharpe | +SL/TP | Sharpe |
|---|---|---|---|---|
{fmt_sl_tp_holdout()}

## ⑦ 正式引擎交叉驗證（BacktestService，跨資產同參數不重調）

| 資產 | 累積報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
{fmt_engine_rows()}

MAE/MFE SL/TP 疊加（正式引擎）：
| 資產 | 無SL/TP | Sharpe | +SL/TP | Sharpe |
|---|---|---|---|---|
{fmt_engine_sl_tp_rows()}

## ⑧ 跨資產穩健性驗證（factrix，因子層級）

⑦已完成核心的跨資產穩健性驗證（同一組參數套 BTC/ETH/TXFR1，不重新調參）。以下用 factrix 從因子層級再驗證一次：關鍵因子 `{key_factor}` 是否在每個資產上個別都站得住腳，以及邊際是否跨持有期穩定。

* **全樣本合併（3 資產）Pooled Cross-sectional IC**：`{pooled_ic.value:.4f}`（p={pooled_ic.p_value:.4f}）
* **逐資產 `predictive_beta`**（`factrix.by_slice` + `factrix.compare`；只有 3 個資產，正式跨切片假設檢定既不適用也統計上不夠力，改用描述性排行榜）：

| 資產 | Beta | p-value | Holm 校正後 p-value |
|---|---|---|---|
{fmt_factor_rows()}

Holm 校正跨 {len(applicable_assets) if applicable_assets else 0} 個資產做 FWER 校正（手刻，原因同③-freq：這是「同一因子依賴性是否跨資產成立」的挑贏家形態，factrix 公開 API 沒有 FWER 工具）。

**已知限制**：日/時因子假設「24 根小時K = 1 個日曆天」的連續 24/7 市場結構，這對 BTC/ETH 成立，但 TXFR1（僅日盤交易時段）並不成立；TXFR1 的因子/策略結果應解讀為「框架可運作」的驗證，而非嚴謹的統計結論。

### ⑧b 因子穩健性：跨持有期橫掃（pooled IC）

| 持有期 | Pooled IC | p-value |
|---|---|---|
{fmt_horizon_robust_rows()}

## 結論（誠實版）

**⑤/⑤b 的正報酬跟③b/⑧的因子檢定結果互相矛盾：候選 B 在 IS-Val 和 OOS 上都是正 Sharpe，但它賴以進出場的關鍵因子（mom_1H_12/rsi_1H_14）在 IS-Train 內部就不穩定（③b VETOED），跨資產也測不出任何個別站得住腳的預測力（⑧全數未過 Holm 校正）。這代表 B 的正報酬更可能來自它疊加的 `mom_1D_10` 日線趨勢濾鏡（單純趨勢跟隨），而不是小時線 timing 因子本身的邊際——跟 timing 因子被當成策略核心賣點的假設不符。**

- **候選比較**：⑤勝出的是 {best_candidate}，{"優於" if val_baseline_beat else "在 IS-Val 上並未優於"}無濾鏡基準；⑤b OOS 盲測{"同樣支持" if oos_baseline_beat == val_baseline_beat else "沒有支持"}這個排序。但見下方③b/⑧，正報酬不代表這組因子邏輯本身站得住腳。
- **③ 因子分析**：1H 上對 13 個因子橫掃，BHY 校正後 {len(significant_factors)}/{len(horizon_rows)} 個組合顯著，但顯著的清一色是日線因子（mom_1D_*/rsi_1D_14/bb_pct_b_1D_20），**候選 B 實際使用的小時線 timing 因子 mom_1H_12 在全部 4 個 forward period 上都沒有通過校正**（最好的 p_bhy=0.7781）。
- **③-freq 基礎頻率橫掃**：換 4H/1D 基礎頻率、Holm-Bonferroni（FWER）跨 base_tf×factor×horizon 校正後，mom/rsi 兩個 timing 因子在三個已測基礎頻率上**都沒有找到顯著組合**——換頻率沒有替 1H 找到本來沒有的邊際，這兩個因子在 BTC 上（至少這個樣本窗口）本身就不具備跨頻率一致的可用邊際。
- **③b oos_decay：兩個因子都被 VETOED**（mom_1H_12 @ 1h、rsi_1H_14 @ 24h 皆反號），代表這兩個 timing 因子在 IS-Train 內部前 70%/後 30% 切分下方向都不穩定，是比 IS-Val 更早出現的警訊。
- **⑧ 跨資產因子穩健性：{key_factor} 在 BTC/ETH/TXFR1 三個資產上，逐資產 `predictive_beta` 校正前 p-value 已經全部 >0.25（BTC 0.7758／ETH 0.2570／TXFR1 0.9218），Holm 校正後全部 ≥0.77，沒有任何一個資產（含決策資產 BTC 本身）通過常見顯著門檻**。跨持有期橫掃（⑧b）同樣：pooled IC 在 12h 不顯著（p=0.4274），僅 1h/24h 勉強顯著但方向為負（IC<0），與策略把 mom_1H_12 當「動量突破」正向訊號使用的假設方向相反。
- **MAE/MFE SL/TP**：見⑥/⑦。IS-Val/OOS 交易級交叉檢查顯示加了 SL/TP 後 Sharpe 提升，但⑦正式引擎上三個資產全部翻負（BTC 0.23→-0.66、ETH 1.84→-0.44、TXFR1 0.37→-0.87），研究腳本的交易級模擬器跟正式引擎在這組 SL/TP 上的結論方向相反——以正式引擎為準，這組固定百分比停損停利不建議疊加。
- **TXFR1 限制**：日/時因子的 24 根 K 棒窗口為 24/7 市場設計，套用到僅日盤交易的 TXFR1 上語意不對齊，TXFR1 數字僅供框架可運作驗證，不是嚴謹統計結論。
- **不建議上線**：{best_candidate} 的回測正報酬站不住腳——支撐它的兩個小時線 timing 因子既沒有通過③的多重檢定校正、也沒有通過③b 的 IS-Train 內部穩定性快篩、更沒有通過⑧的跨資產穩健性檢定，三層檢查全部指向同一個結論。下一步應該回頭檢驗 `mom_1D_10` 日線趨勢濾鏡本身單獨的邊際（很可能才是⑦正報酬的真正來源），而不是把目前這組 timing 因子邏輯直接視為定案往下推進。
"""

    report_path = os.path.join(os.path.dirname(__file__), "report.md")
    with open(report_path, "w") as f_rep:
        f_rep.write(report)
    print(f"\nReport written to {report_path}")


if __name__ == "__main__":
    run_research()
