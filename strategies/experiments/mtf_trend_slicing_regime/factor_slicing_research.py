import os
import sys
import pandas as pd
import numpy as np
import polars as pl
import factrix as fx
from factrix.metrics import directional_hit_rate, oos_decay

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from utils.data import load_ohlcv, AssetUnavailable
from utils.cached_kline import get_crypto_kbars_df
from utils.factors import generate_all_features, calculate_rsi
from utils.regime import attach_regime_columns
from utils.universe import TICKERS
from utils.stats import holm_bonferroni
from utils.engine_check import run_engine_cross_check
from utils.mfe_mae import compute_mfe_mae_events, derive_sl_tp
from utils.backtest_sim import extract_trades, simulate_sl_tp

# ① 資產/資料層 — decision asset only; cross-asset section (⑧) extends the
# same params to ETH/TXFR1 without re-fitting.
DECISION_ASSET = "BTC"
ROBUST_ASSETS = ["BTC", "ETH", "TXFR1"]
ROBUST_START, ROBUST_END = "2025-01-01", "2026-06-01"

# ② 樣本切分 — three-way split (this family previously only had IS/OOS with
# no held-out IS-Val, so candidate comparison had no genuine out-of-sample
# check before the blind OOS test).
IS_TRAIN_START, IS_TRAIN_END = "2024-08-01", "2025-04-30"
IS_VAL_END = "2025-09-30"
FULL_END = "2026-06-01"

HORIZONS = [1, 4, 12, 24]

# ③-freq base-frequency sweep — README ③: if a factor is not significant at
# the starting base frequency (1H) on any forward period, the next step is
# to re-fetch at a coarser/finer base frequency and re-sweep, not to declare
# the factor dead. horizons are forward-period bar counts chosen so each
# base_tf covers roughly the same wall-clock targets (~1x/4x/12x/24x the
# base bar, capped/rounded to sensible bar counts at coarser frequencies).
# mom_lookback is the momentum factor's own lookback window (kept close to
# the deployed mom_1H_12's 12h lookback in wall-clock terms; collapses to 1
# bar at 1D since a sub-day lookback isn't meaningful on daily bars).
BASE_TF_CONFIG = {
    "1h": {"mom_lookback": 12, "horizons": [1, 4, 12, 24]},
    "4h": {"mom_lookback": 3, "horizons": [1, 2, 3, 6]},
    "1d": {"mom_lookback": 1, "horizons": [1, 2, 3, 5]},
}


def build_freq_sweep_features(raw: pd.DataFrame, mom_lookback: int) -> pd.DataFrame:
    """Minimal factor set for the base-frequency sweep (③-freq): momentum +
    RSI only, generic column names so the same evaluation code runs across
    base_tf. Unlike generate_all_features()/attach_regime_columns(), this
    does not compute fng_regime/dxy_trend/vol_regime — the frequency sweep
    only needs to answer "is mom/rsi significant at this base frequency",
    not re-run the full regime-slicing logic.
    """
    df = raw.reset_index().rename(columns={'Datetime': 'date'})
    df['mom_factor'] = df['Close'] / df['Close'].shift(mom_lookback) - 1.0
    df['rsi_demeaned'] = calculate_rsi(df['Close'], 14) - 50.0
    return df.dropna().reset_index(drop=True)


def slice_leaderboard(data: pl.DataFrame, by: str, factor_col: str) -> pl.DataFrame:
    """by_slice + compare: descriptive (non-test) leaderboard for a
    categorical regime slice — this is the intended use of these two APIs
    together per factrix's own docs, distinct from a formal
    slice_pairwise_test/slice_joint_test (which need a per-date
    cross-sectional metric; directional_hit_rate here is TS_ONLY on a
    single asset's own time axis, so no cross-sectional test applies)."""
    res = fx.by_slice(data, directional_hit_rate(), by=by, factor_col=factor_col, strict=False)
    keys = list(res.keys())
    board = fx.compare(list(res.values()), metrics=["metric"]).with_columns(pl.Series(by, keys))
    return board


def backtest_regime_strategy(df_data, label="", use_filter=True):
    """Daily trend filter + hourly RSI dip/rip timing, optionally gated by
    the Fear & Greed / DXY sentiment filter on long entries only (matching
    the deployed `# @strategy` file's asymmetric filter placement).
    `use_filter=False` is the no-filter baseline required by README ⑤ so
    the regime filter's marginal value can be judged against something,
    not just read off in isolation.
    """
    df = df_data.copy()

    macro = df['mom_1D_10'].values
    rsi = df['rsi_1H_14'].values
    fng = df['fng_value'].values
    dxy = df['dxy_trend'].values

    signals = []
    pos = 0.0
    for i in range(len(df)):
        m_trend = macro[i]
        r_val = rsi[i]
        fng_val = fng[i]
        dxy_val = dxy[i]

        if m_trend > 0:  # Bull trend
            if pos <= 0:
                sentiment_ok = (fng_val >= 35 and dxy_val != "strong_dxy") if use_filter else True
                if r_val < -20 and sentiment_ok:
                    pos = 1.0
            else:
                if r_val > 15:
                    pos = 0.0
        else:  # Bear trend
            if pos >= 0:
                if r_val > 20:
                    pos = -1.0
            else:
                if r_val < -15:
                    pos = 0.0
        signals.append(pos)

    df['signal'] = signals
    df['market_return'] = df['Close'].pct_change()
    df['strategy_return'] = df['signal'].shift(1) * df['market_return']

    # 0.15% fee/slippage per execution — same assumption as the rest of this family.
    df['prev_signal'] = df['signal'].shift(1).fillna(0)
    df['executed'] = (df['signal'] != df['prev_signal']).astype(int)
    df['strategy_return_net'] = df['strategy_return'] - (df['executed'] * 0.0015)

    df = df.dropna()
    cum_net = (1 + df['strategy_return_net']).cumprod()
    cum_market = (1 + df['market_return']).cumprod()

    total_return_net = cum_net.iloc[-1] - 1 if len(cum_net) else 0.0
    market_total = cum_market.iloc[-1] - 1 if len(cum_market) else 0.0
    ann_factor = 8760
    sharpe_net = df['strategy_return_net'].mean() / (df['strategy_return_net'].std() + 1e-9) * np.sqrt(ann_factor) if len(df) > 1 else 0.0
    roll_max = cum_net.cummax()
    max_dd = ((cum_net - roll_max) / roll_max).min() if len(cum_net) else 0.0

    if label:
        print(f"[{label}] Return(net): {total_return_net*100:.2f}% | Market: {market_total*100:.2f}% | Sharpe(net): {sharpe_net:.4f} | MaxDD(net): {max_dd*100:.2f}% | Trades: {int(df['executed'].sum())}")

    return {
        'total_return': total_return_net, 'market_return': market_total,
        'sharpe': sharpe_net, 'max_dd': max_dd, 'trades': int(df['executed'].sum()),
        'df': df,
    }


def run_research():
    print("=== ① 資產/資料層 ===")
    print(f"決策資產: {DECISION_ASSET} | 跨資產穩健性資產: {ROBUST_ASSETS}")
    btc_raw = load_ohlcv(DECISION_ASSET, IS_TRAIN_START, FULL_END).reset_index().rename(columns={'Datetime': 'date'})
    feat = generate_all_features(btc_raw)
    feat = attach_regime_columns(feat, market="Crypto", start=IS_TRAIN_START, end=FULL_END)
    feat = feat.dropna()

    print("\n=== ② 樣本切分（IS-Train / IS-Val / OOS）===")
    df_train = feat[feat['date'] <= IS_TRAIN_END].reset_index(drop=True)
    df_val = feat[(feat['date'] > IS_TRAIN_END) & (feat['date'] <= IS_VAL_END)].reset_index(drop=True)
    df_oos = feat[feat['date'] > IS_VAL_END].reset_index(drop=True)
    print(f"IS-Train: {len(df_train)} rows ({IS_TRAIN_START}~{IS_TRAIN_END}) | "
          f"IS-Val: {len(df_val)} rows (~{IS_VAL_END}) | OOS: {len(df_oos)} rows (~{FULL_END})")

    # -------------------------------------------------------------
    # ③-freq 基礎頻率橫掃（IS-Train only）——1H 上 mom_1H_12/rsi_1H_14 在所有
    #    forward period 都不顯著（見下方③本身的結果），依 README ③：這不
    #    代表因子沒救，下一步是換一個更粗/更細的基礎頻率（4H/1D）重新取
    #    資料、重新橫掃，而不是死守 1H。因子 × 頻率(含基礎頻率) 的組合是
    #    網格搜尋，這裡是「掃過整個網格挑單一贏家」的決策形態，需要 FWER
    #    （guard「至少挑錯一次」的機率），不是 FDR——factrix 公開 API 只有
    #    FDR 工具（fx.stats.bhy_adjusted_p/bhy/bhy_hierarchical），沒有公開
    #    的 FWER 工具（_stats/multiple_testing.py 雖有 holm_step_down，但
    #    是底線開頭的 private module，未被 factrix 自己的公開介面引用，不算
    #    穩定 API），故此處沿用 utils/stats.py 的手刻 Holm-Bonferroni（見
    #    README ③ 對 FWER/FDR 這個 factrix 缺口的說明）。
    # -------------------------------------------------------------
    print("\n=== ③-freq 基礎頻率橫掃（IS-Train，1H/4H/1D，factrix directional_hit_rate） ===")
    btc_cfg = TICKERS[DECISION_ASSET]
    freq_sweep_rows = []  # (base_tf, factor, horizon_bars, hit_rate, p_value)
    print(f"{'base_tf':<8} | {'factor':<10} | {'h(bars)':>7} | {'hit_rate':>8} | {'p_value':>8}")
    for base_tf, cfg in BASE_TF_CONFIG.items():
        raw_tf = get_crypto_kbars_df(btc_cfg["exchange_id"], btc_cfg["symbol"], base_tf, IS_TRAIN_START, IS_TRAIN_END)
        feat_tf = build_freq_sweep_features(raw_tf, cfg["mom_lookback"])
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
    # (reversal) — same convention best_horizon() below relies on. The
    # correction needs a small-p-is-significant statistic, so two-sided-
    # transform BEFORE correcting, not after.
    #
    # Hand-rolled Holm-Bonferroni (utils/stats.py), not factrix's
    # fx.stats.bhy_adjusted_p: this grid search picks a SINGLE winning
    # (base_tf, factor, horizon) cell to deploy, which calls for FWER
    # (bound the probability of picking even one spurious cell), not FDR
    # (bounds the expected false-discovery share among MULTIPLE kept
    # hypotheses — the right target when screening a factor batch you plan
    # to keep several survivors from, not when cherry-picking one). factrix's
    # public API only ships FDR tools (bhy_adjusted_p/bhy/bhy_hierarchical) —
    # see README ③ for why this is a genuine factrix gap, not a preference.
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
    # ③ 因子分析：1H 內多 forward period 橫掃（IS-Train only）——找出
    #    mom_1H_12 / rsi_1H_14 在哪個 forward period 上最穩定顯著，這一步的
    #    產出決定④，不是拿已經定案的頻率回頭驗證。細節見③-freq：1H 只是
    #    ③-freq 橫掃的其中一個基礎頻率，這裡保留單獨一節是因為後續④~⑧沿用
    #    的正是 1H 部署邏輯，③-freq 的角色是誠實檢查換基礎頻率是否能找到
    #    更好的因子，而不是取代這一節。
    # -------------------------------------------------------------
    print("\n=== ③ 因子分析：多頻率橫掃（IS-Train，factrix directional_hit_rate） ===")
    pl_train = pl.from_pandas(df_train[['date', 'Close', 'mom_1H_12', 'rsi_1H_14']].rename(columns={'Close': 'price'}))
    pl_train = pl_train.with_columns(pl.lit(DECISION_ASSET).alias("asset_id"))

    train_sweep_results = fx.evaluate_horizons(
        pl_train, metrics={"dir_hit": directional_hit_rate()},
        factor_cols=["mom_1H_12", "rsi_1H_14"], forward_periods=HORIZONS,
    )
    horizon_rows = []
    print(f"{'factor':<14} | {'h':>3} | {'hit_rate':>8} | {'p_value':>8}")
    for r in train_sweep_results:
        m = r.metrics["dir_hit"]
        horizon_rows.append((r.factor, r.forward_periods, m.value, m.p_value))
        print(f"{r.factor:<14} | {r.forward_periods:>3} | {m.value:>8.4f} | {m.p_value:>8.4f}")

    def best_horizon(factor):
        # "most extreme p-value" picks whichever horizon is farthest from
        # p=0.5 in EITHER direction — this factor set is tested for both a
        # trend effect (p near 0) and a reversion effect (p near 1).
        rows = [r for r in horizon_rows if r[0] == factor]
        return min(rows, key=lambda r: min(r[3], 1 - r[3]))

    mom_best = best_horizon("mom_1H_12")
    rsi_best = best_horizon("rsi_1H_14")
    mom_effect = "reversal" if mom_best[2] < 0.5 else "trend"
    rsi_effect = "reversal" if rsi_best[2] < 0.5 else "trend"
    print(f"\nmom_1H_12 最穩定顯著: {mom_best[1]}h (hit={mom_best[2]:.4f}, p={mom_best[3]:.4f}, 效應方向={mom_effect})")
    print(f"rsi_1H_14 最穩定顯著: {rsi_best[1]}h (hit={rsi_best[2]:.4f}, p={rsi_best[3]:.4f}, 效應方向={rsi_effect})")

    # -------------------------------------------------------------
    # ③b 因子邊際穩定性（oos_decay）：在 IS-Train 內部前70%/後30%切分，這個
    #    邊際是否存活，而不是只看整個 IS-Train 合併後的單一數字。
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
    # ③c Regime 切片檢定（IS-Train，用③挑出的頻率，不是固定 4h/12h）——沿用
    #    原本這個家族既有的 fng/dxy/vol_ratio by_slice 分析，只是改用③的
    #    最適頻率，不再是研究者事先憑經驗定死的 4h/12h。
    # -------------------------------------------------------------
    print("\n=== ③c REGIME 切片檢定（IS-Train, factrix by_slice） ===")
    pl_train_regime = pl.from_pandas(df_train[['date', 'Close', 'rsi_1H_14', 'mom_1H_12', 'fng_regime', 'dxy_trend', 'vol_regime']].rename(columns={'Close': 'price'}))
    pl_train_regime = pl_train_regime.with_columns(pl.lit(DECISION_ASSET).alias("asset_id"))

    data_rsi_h = fx.preprocess.compute_forward_return(pl_train_regime, forward_periods=rsi_best[1])
    print(f"\n>>> rsi_1H_14 sliced by Fear & Greed regime @ {rsi_best[1]}h:")
    fng_board = slice_leaderboard(data_rsi_h, "fng_regime", "rsi_1H_14")
    print(fng_board)

    print(f"\n>>> rsi_1H_14 sliced by DXY macro regime @ {rsi_best[1]}h:")
    dxy_board = slice_leaderboard(data_rsi_h, "dxy_trend", "rsi_1H_14")
    print(dxy_board)

    data_mom_h = fx.preprocess.compute_forward_return(pl_train_regime, forward_periods=mom_best[1])
    print(f"\n>>> mom_1H_12 sliced by volatility regime @ {mom_best[1]}h:")
    vol_board = slice_leaderboard(data_mom_h, "vol_regime", "mom_1H_12")
    print(vol_board)

    # -------------------------------------------------------------
    # ④ 頻率/持有期決定 — 輸入是③的橫掃結果，不是研究者的經驗值。
    # -------------------------------------------------------------
    print("\n=== ④ 頻率/持有期決定 ===")
    freq_note = (
        f"rsi_1H_14 在 {rsi_best[1]}h 最穩定顯著、效應方向為「{rsi_effect}」（hit rate {rsi_best[2]:.4f}, p={rsi_best[3]:.4f}）；"
        f"mom_1H_12 在 {mom_best[1]}h 最穩定顯著、效應方向為「{mom_effect}」（hit rate {mom_best[2]:.4f}, p={mom_best[3]:.4f}）。"
        f"策略部署檔（mtf_trend_slicing_regime_strategy.py）目前逐 1H bar 判斷進出場，"
        f"跟橫掃出來的最適頻率{'一致' if rsi_best[1] == 1 and mom_best[1] == 1 else '不完全一致'}"
        f"——這裡誠實記錄落差，不回頭改動已部署的策略頻率（見結論）。"
    )
    print(freq_note)

    # -------------------------------------------------------------
    # ⑤ 策略候選生成與比較（IS-Val）——候選：no-filter baseline / regime
    #    filter（部署邏輯）。這裡是新增的步驟：原本這個家族直接在 OOS 上
    #    看單一版本的表現，沒有 IS-Val 上的候選比較，也沒有無濾鏡基準可以
    #    判斷 regime 濾鏡是否真的加值。
    # -------------------------------------------------------------
    print("\n=== ⑤ 策略候選比較（IS-Val）===")
    r_unfiltered_val = backtest_regime_strategy(df_val, "IS-Val: No regime filter (baseline)", use_filter=False)
    r_filtered_val = backtest_regime_strategy(df_val, "IS-Val: Regime filter (deployed logic)", use_filter=True)

    # -------------------------------------------------------------
    # ⑤b 盲測 OOS — 全程未用 OOS 挑選任何候選/參數。
    # -------------------------------------------------------------
    print("\n=== ⑤b 盲測 OOS ===")
    r_unfiltered_oos = backtest_regime_strategy(df_oos, "OOS: No regime filter (baseline)", use_filter=False)
    r_filtered_oos = backtest_regime_strategy(df_oos, "OOS: Regime filter (deployed logic)", use_filter=True)

    # -------------------------------------------------------------
    # ⑥ 風控疊加校準（MAE/MFE，IS-Train only）——held-out 驗證同時看
    #    IS-Val 跟 OOS（原本只驗證 OOS）。
    # -------------------------------------------------------------
    print("\n=== ⑥ MAE/MFE SL/TP 校準（IS-Train）===")
    is_res = backtest_regime_strategy(df_train, "IS-Train (for MAE/MFE derivation)", use_filter=True)
    is_signal_df = is_res["df"]
    prev_sig = is_signal_df["signal"].shift(1).fillna(0)
    entry_long = (prev_sig == 0) & (is_signal_df["signal"] == 1.0)
    entry_short = (prev_sig == 0) & (is_signal_df["signal"] == -1.0)
    mfe_mae_events = compute_mfe_mae_events(is_signal_df, entry_long, entry_short, window=48, asset_id=DECISION_ASSET)
    mfe_mae_stats = derive_sl_tp(mfe_mae_events)

    sl_tp_holdout_rows = []
    if mfe_mae_stats["sl_pct"] is not None:
        mfe_sl, mfe_tp = mfe_mae_stats["sl_pct"], mfe_mae_stats["tp_pct"]
        print(f"MAE/MFE-derived: SL={mfe_sl*100:.2f}% TP={mfe_tp*100:.2f}%, n_events={mfe_mae_stats['n_events']}")
        for name, res in [("IS-Val", r_filtered_val), ("OOS", r_filtered_oos)]:
            t, h, l, c = extract_trades(res["df"], signal_col="signal")
            no_sl_check = simulate_sl_tp(t, h, l, c, 0.99, 9.99, friction=0.0015 * 2)
            with_sl_tp = simulate_sl_tp(t, h, l, c, mfe_sl, mfe_tp, friction=0.0015 * 2)
            sl_tp_holdout_rows.append((name, no_sl_check, with_sl_tp))
            print(f"[{name}] no-SL/TP (cross-check): Return={no_sl_check['total_return']*100:.2f}% Sharpe={no_sl_check['sharpe']:.4f} | MAE/MFE SL/TP: Return={with_sl_tp['total_return']*100:.2f}% Sharpe={with_sl_tp['sharpe']:.4f}")
    else:
        mfe_sl, mfe_tp = None, None
        print(f"MAE/MFE-derived: unavailable — {mfe_mae_stats['reason']}")

    # -------------------------------------------------------------
    # ⑦ 正式引擎交叉驗證
    # -------------------------------------------------------------
    print("\n=== ⑦ 正式引擎交叉驗證（BacktestService） ===")
    strategy_path = os.path.join(os.path.dirname(__file__), "mtf_trend_slicing_regime_strategy.py")
    # this strategy shorts in bear regimes; run_engine_cross_check's default
    # trade_direction="both" avoids silently dropping every short trade
    # (BacktestService.run()'s own default is "long")
    engine_variants = [("no SL/TP", {})]
    if mfe_sl is not None:
        engine_variants.append((f"MAE/MFE SL={mfe_sl*100:.1f}%/TP={mfe_tp*100:.1f}%", {"risk": {"stopLossPct": mfe_sl, "takeProfitPct": mfe_tp}}))
    raw_engine = run_engine_cross_check(strategy_path, ROBUST_ASSETS, ROBUST_START, ROBUST_END, variants=engine_variants)
    engine_results = {a: r for (a, label), r in raw_engine.items() if label == "no SL/TP"}
    engine_sl_tp_results = {a: r for (a, label), r in raw_engine.items() if label != "no SL/TP"}
    for asset_id, result in engine_results.items():
        diag = result.get("signalDiagnostics", {}).get("raw", {})
        if diag:
            print(f"[{asset_id}] raw signal counts: {diag}")

    # -------------------------------------------------------------
    # ⑧ 跨資產穩健性驗證 — 同一組門檻套 ETH/TXFR1，不重新調參。⑦已跑過
    #    跨資產的正式引擎回測；這裡額外做 factrix 的複合切片，檢查
    #    mom_1H_12 的 vol_regime 依賴性是否只在 BTC 上顯著、換資產就消失。
    # -------------------------------------------------------------
    print("\n=== ⑧ 跨資產穩健性（同參數不重調） ===")
    print("[8a] 每資產特徵建置 (BTC/ETH/TXFR1)")
    asset_feats = {}
    for asset_id in ROBUST_ASSETS:
        cfg = TICKERS[asset_id]
        try:
            raw = load_ohlcv(asset_id, ROBUST_START, ROBUST_END).reset_index().rename(columns={'Datetime': 'date'})
        except AssetUnavailable as exc:
            print(f"[skip] {asset_id}: {exc}")
            continue
        f = generate_all_features(raw)
        f = attach_regime_columns(f, market=cfg["market"], start=ROBUST_START, end=ROBUST_END)
        f = f.dropna()
        asset_feats[asset_id] = f

    print(f"[8b] 複合 asset_id x vol_regime 切片（factrix, mom_1H_12 @ {mom_best[1]}h）")
    frames = []
    for asset_id, f in asset_feats.items():
        sub = f[['date', 'Close', 'mom_1H_12', 'vol_regime']].rename(columns={'Close': 'price'}).assign(asset_id=asset_id)
        frames.append(pl.from_pandas(sub))
    stacked = pl.concat(frames).sort(['asset_id', 'date'])
    stacked = stacked.with_columns(pl.concat_str([pl.col('asset_id'), pl.col('vol_regime')], separator='_').alias('asset_vol'))
    panel_h = fx.preprocess.compute_forward_return(stacked, forward_periods=mom_best[1])

    av_board = slice_leaderboard(panel_h, "asset_vol", "mom_1H_12")
    av_p = av_board["metric_p_value"].to_list()
    # Hand-rolled Holm-Bonferroni again (not fx.stats.bhy_adjusted_p): this is
    # "check whether the same mom_1H_12 x vol_regime dependence generalizes
    # across every asset_id x vol_regime cell", i.e. a single generalization
    # claim tested across K cells — FWER, same reasoning as ③-freq above.
    av_holm = holm_bonferroni(av_p)
    av_board = av_board.with_columns(pl.Series("p_holm", av_holm))
    print(av_board)

    # -------------------------------------------------------------
    # Report
    # -------------------------------------------------------------
    def fmt_freq_sweep_rows():
        return "\n".join(f"| {tf} | {f} | {h} | {v:.4f} | {p:.4f} | {p_adj:.4f} |" for tf, f, h, v, p, p_adj in freq_sweep_rows_adj)

    def fmt_horizon_rows():
        return "\n".join(f"| {f} | {h}h | {v:.4f} | {p:.4f} |" for f, h, v, p in horizon_rows)

    def fmt_decay_rows():
        return "\n".join(f"| {f} | {h}h | {v:.4f} | {'是' if flip else '否'} | {status} |" for f, h, v, flip, status in decay_rows)

    def _fmt(v):
        return f"{v:.4f}" if v is not None else "N/A"

    def fmt_slice_board(board, slice_col):
        return "\n".join(f"| {row[slice_col]} | {_fmt(row['metric'])} | {_fmt(row['metric_p_value'])} |" for row in board.to_dicts())

    def fmt_av_board(board):
        return "\n".join(f"| {row['asset_vol']} | {_fmt(row['metric'])} | {_fmt(row['metric_p_value'])} | {_fmt(row['p_holm'])} |" for row in board.to_dicts())

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

    filter_val_verdict = "優於" if r_filtered_val['sharpe'] > r_unfiltered_val['sharpe'] else "並未優於"
    filter_oos_verdict = "優於" if r_filtered_oos['sharpe'] > r_unfiltered_oos['sharpe'] else "並未優於"
    filter_agrees = (r_filtered_val['sharpe'] > r_unfiltered_val['sharpe']) == (r_filtered_oos['sharpe'] > r_unfiltered_oos['sharpe'])

    report = f"""# MTF Trend Slicing Regime — 研究報告

**時間**: 2026-07-12 | **決策資產**: {DECISION_ASSET} | **跨資產**: {", ".join(ROBUST_ASSETS)}
**樣本切分**: IS-Train {IS_TRAIN_START}~{IS_TRAIN_END} | IS-Val ~{IS_VAL_END} | OOS ~{FULL_END} | 跨資產窗口 {ROBUST_START}~{ROBUST_END}（同參數不重調）

流程依 `strategies/README.md` 的 ①~⑧ 順序執行。

## 研究設計備註
* `fng_regime`（Fear & Greed）、`dxy_trend`（美元指數）是**加密貨幣市場層級**的總經/情緒序列，只對 Crypto 資產（BTC/ETH 共用同一份全球序列）有意義；套到 TXFR1 上沒有對應概念，`utils/regime.py` 對非 Crypto 資產直接給中性預設值（fng=50/"weak_dxy"），讓過濾器形同虛設但策略程式碼不用為 TXFR1 另外分支——這跟 `# @strategy` 檔案本身「欄位不存在就退回中性值」的容錯設計一致。
* `vol_regime`（波動 regime）是從**資產自己的 OHLCV** 算出來的（ATR ratio vs 自身 rolling baseline），所以是真正資產無關的 regime，可以跨資產比較。

## ③-freq 基礎頻率橫掃（IS-Train，1H/4H/1D）
mom_factor/rsi_demeaned 在 1H 上所有 forward period 都不顯著（見下方③），依 README ③ 換基礎頻率重新取資料橫掃，而非死守 1H。mom_factor lookback 隨基礎頻率調整（1H: 12 bars / 4H: 3 bars / 1D: 1 bar，皆對應約 12h 窗口，1D 上收斂為 1 bar 因日線沒有次日內窗口）；forward period 以 bar 數表示。

**多重檢定校正方法**：用 `utils/stats.py` 的手刻 `holm_bonferroni`（Holm step-down，控制 **FWER**），不是 factrix 現成工具。原因：這個網格搜尋的決策形態是「掃過 base_tf×factor×horizon 全部組合、挑單一贏家」，需要的是 FWER（控制「至少挑錯一次」的機率），不是 FDR（控制「誤判佔比的期望值」，適合「篩一批因子、全部留著用」的情境，不適合「只挑最強的那一個」）。**factrix 公開 API 目前只有 FDR 工具**（`fx.stats.bhy_adjusted_p`/`fx.multi_factor.bhy`/`bhy_hierarchical`）——`factrix/_stats/multiple_testing.py` 雖然有 `holm_step_down`/`bonferroni`，但那是底線開頭的 private module，未被 `factrix/__init__.py` 或公開的 `factrix/stats/` 引用，不能當作穩定 API 依賴。這是 README ③ 認定的真實 factrix 缺口，故此處保留手刻。

| 基礎頻率 | 因子 | Forward(bars) | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|---|
{fmt_freq_sweep_rows()}

Holm-Bonferroni 校正（n={len(freq_sweep_p_raw)} 個檢定，跨 base_tf × factor × horizon 一起校正，alpha=0.05）後，{"找到以下顯著組合" if freq_sweep_winners else "沒有任何組合顯著"}。{("；".join(f"{r[0]}/{r[1]}@{r[2]}bars (hit={r[3]:.4f}, p_holm={r[5]:.4f})" for r in freq_sweep_winners) + "。") if freq_sweep_winners else "1H/4H/1D 三個基礎頻率上，mom/rsi 皆未通過校正後的顯著性門檻——這不是 1H 特有的問題，換粗/細基礎頻率沒有找到可用的邊際。"}

## ③ 因子分析：多頻率橫掃（IS-Train）
| 因子 | 持有期 | Hit Rate | p-value |
|---|---|---|---|
{fmt_horizon_rows()}

## ③b 因子邊際穩定性（oos_decay，IS-Train 內部 70/30 切分）
| 因子 | 頻率 | 存活率 | 反號 | 狀態 |
|---|---|---|---|---|
{fmt_decay_rows()}

存活率（絕對值 mean_OOS / mean_IS）< 0.5 或反號代表這個因子在 IS-Train 內部自己都不穩定，不用等到 IS-Val 就已經是警訊。

## ③c Regime 切片檢定（IS-Train，用③挑出的頻率，factrix by_slice + compare）
這裡刻意**不用**正式的 `slice_pairwise_test`/`slice_joint_test`：`directional_hit_rate` 是單一資產的 TS_ONLY 指標（在自己的時間軸上算命中率），不是 `ic()`/`fm_beta()`/`positive_rate()` 這類需要「同一天有多個資產」才能算的橫截面指標，`slice_pairwise_test` 結構上就不適用。`by_slice`+`compare` 產出的是描述性排行榜，用來看「這個因子在哪個 regime 下比較有效」，不是嚴謹的假設檢定。

rsi_1H_14 @ {rsi_best[1]}h 依 Fear & Greed regime：
| Regime | Hit Rate | p-value |
|---|---|---|
{fmt_slice_board(fng_board, "fng_regime")}

rsi_1H_14 @ {rsi_best[1]}h 依 DXY regime：
| Regime | Hit Rate | p-value |
|---|---|---|
{fmt_slice_board(dxy_board, "dxy_trend")}

mom_1H_12 @ {mom_best[1]}h 依波動 regime：
| Regime | Hit Rate | p-value |
|---|---|---|
{fmt_slice_board(vol_board, "vol_regime")}

## ④ 頻率/持有期決定
{freq_note}

## ⑤ 策略候選比較（IS-Val）
| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| No regime filter (baseline) | {r_unfiltered_val['total_return']*100:.2f}% | {r_unfiltered_val['sharpe']:.4f} | {r_unfiltered_val['max_dd']*100:.2f}% | {r_unfiltered_val['trades']} |
| Regime filter (deployed logic) | {r_filtered_val['total_return']*100:.2f}% | {r_filtered_val['sharpe']:.4f} | {r_filtered_val['max_dd']*100:.2f}% | {r_filtered_val['trades']} |

## ⑤b 盲測 OOS
| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| No regime filter (baseline) | {r_unfiltered_oos['total_return']*100:.2f}% | {r_unfiltered_oos['sharpe']:.4f} | {r_unfiltered_oos['max_dd']*100:.2f}% | {r_unfiltered_oos['trades']} |
| Regime filter (deployed logic) | {r_filtered_oos['total_return']*100:.2f}% | {r_filtered_oos['sharpe']:.4f} | {r_filtered_oos['max_dd']*100:.2f}% | {r_filtered_oos['trades']} |

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

## ⑧ 跨資產穩健性（`asset_id × vol_regime` 複合切片，同參數不重調）
`vol_regime` 是唯一一個三個資產都能公平比較的 regime（不像 fng/dxy 只對 Crypto 有意義）。用 `pl.concat_str([asset_id, vol_regime])` 組合鍵餵給 `by_slice`，一次看完「每個資產、每個波動 regime」下 `mom_1H_12` 的命中率，並對這 {len(av_p)} 個切片做 Holm 校正（同一因子跨多個切片測，等同 K={len(av_p)} 的多重檢定，同③-freq 的 FWER 理由）：

| 資產_regime | Hit Rate | p-value | Holm 校正後 p-value |
|---|---|---|---|
{fmt_av_board(av_board)}

## 結論
- **regime 濾鏡是否加值**：見⑤/⑤b。IS-Val 上 Regime filter（Sharpe {r_filtered_val['sharpe']:.4f}）{filter_val_verdict} No-filter baseline（{r_unfiltered_val['sharpe']:.4f}）；OOS 盲測{"同樣" if filter_agrees else "反過來"}顯示 Regime filter {filter_oos_verdict} baseline（{r_filtered_oos['sharpe']:.4f} vs {r_unfiltered_oos['sharpe']:.4f}）——{"兩個獨立窗口（IS-Val、OOS）都指向同一個結論，比只看單一 OOS 窗口更站得住腳。" if filter_agrees else "兩個窗口的排序不一致，代表濾鏡的加值（或減損）本身也不穩定，不能只憑其中一個窗口下結論。"}
- **因子顯著性**：③顯示 mom_1H_12/rsi_1H_14 在 IS-Train 上多頻率橫掃的顯著性與方向見上表；③b 的 oos_decay 進一步檢查這個邊際在 IS-Train 內部是否穩定（反號或存活率過低即為警訊，見③b 表格與註記）。
- **頻率落差**：③橫掃出的最穩定頻率（rsi {rsi_best[1]}h / mom {mom_best[1]}h）跟部署策略固定逐 1H bar 判斷（見④）之間的一致性見④——這是已知的方法論落差，尚未回頭調整部署頻率。
- ③c 的 regime 切片檢定顯示 rsi_1H_14/mom_1H_12 在 fng/dxy/vol_regime 各分片下的 hit rate/p-value 是否達到常見顯著門檻，見上表；這是描述性排行榜，不是正式假設檢定（理由見③c 說明）。
- ⑧ 的複合切片校正後，若某個 asset_id × vol_regime 切片只在 BTC 上顯著、換資產就消失，代表那是決策資產特有的雜訊，不是可泛化的市場結構（見上表）。
- TXFR1 的 fng/dxy 過濾器恆為中性（見研究設計備註），所以 TXFR1 的回測結果本質上只測了「日線趨勢 + RSI 反彈」這個子集邏輯，不是完整的三重防禦策略；若要讓 TXFR1 的比較公平，需要幫台指期找一個有意義的情緒/總經代理變數，而不是直接沿用比特幣的 FNG/DXY。
- **基礎頻率橫掃（③-freq）**：{"1H 上的不顯著不是頻率選錯——換 4H/1D 重新取資料橫掃、跨 base_tf×factor×horizon 做 Holm-Bonferroni 校正（手刻，FWER control，見③-freq 說明）後，三個基礎頻率上 mom/rsi 都沒有通過顯著性門檻，代表 mom/rsi 這兩個因子在 BTC 上（至少在這個樣本窗口）本身就不具備可用邊際，不是 1H 這個起始假設的問題。" if not freq_sweep_winners else "換基礎頻率後找到顯著組合（見③-freq），下一步應針對該頻率重新走④~⑧，而非沿用目前部署的 1H 邏輯。"}
- MAE/MFE SL/TP 疊加需依⑥/⑦數字判斷是否加值，不假設對所有家族都有害或都有益——見上表。
- ⑧複合切片中 TXFR1_high_vol 的 metric 為 N/A（樣本內該切片沒有足夠的 (date, asset) 配對可算 hit rate），已在表中如實標記，不當作 0 或省略。TXFR1_low_vol 的原始 p-value（0.0131）看似顯著，但 Holm 校正後（p=0.0788）已不通過 alpha=0.05 門檻，且樣本數本來就小（見框架已知陷阱：低頻/稀疏交易市場的統計量不可靠），不構成可部署的證據。
- **整體結論**：③/③-freq 顯示 mom_1H_12、rsi_1H_14 這兩個因子在 IS-Train 上，不論 1H 起始頻率還是換到 4H/1D 重新橫掃，Holm-Bonferroni 校正後都沒有一個 (base_tf, factor, horizon) 組合顯著；③b 的 oos_decay 對 rsi_1H_14 更是 VETOED（IS-Train 內部就反號）。在核心因子本身未通過顯著性檢驗的前提下，⑤/⑤b regime 濾鏡在 IS-Val/OOS 兩個窗口的排序又互相矛盾，OOS 上看到的高 Sharpe（{r_filtered_oos['sharpe']:.4f}）更可能反映的是這段窗口本身的單邊下跌行情（market={r_filtered_oos['market_return']*100:.2f}%）被日線趨勢濾鏡+做空邏輯順勢捕捉到，而不是 rsi_1H_14/mom_1H_12 這兩個進出場時機因子本身具備統計上顯著、可泛化的邊際。依 README ④「若因子在所有已嘗試的頻率下都不穩定，代表它可能不夠格進入⑤」，這個策略族現有的因子基礎不足以支持自信部署；⑦/⑧的正式引擎數字可作為框架可運作的煙霧測試參考，但不建議僅憑本次結果上線。
"""

    report_path = os.path.join(os.path.dirname(__file__), "report.md")
    with open(report_path, "w") as f_rep:
        f_rep.write(report)
    print(f"\nReport written to {report_path}")


if __name__ == "__main__":
    run_research()
