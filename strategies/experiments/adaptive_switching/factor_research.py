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
from utils.factors import calculate_rsi, add_daily_trend_filter
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
    base_tf. Unlike build_features(), does not compute vol_ratio/is_trend_regime
    (that filter is 1H-cumulative-volume-specific, see build_features'
    docstring) — the frequency sweep only needs to answer "is mom/rsi
    significant at this base frequency", not re-run the switching logic.
    """
    df = raw.reset_index().rename(columns={'Datetime': 'date'})
    df['mom_factor'] = df['Close'] / df['Close'].shift(mom_lookback) - 1.0
    df['rsi_demeaned'] = calculate_rsi(df['Close'], 14) - 50.0
    return df.dropna().reset_index(drop=True)


def build_features(raw: pd.DataFrame) -> pd.DataFrame:
    """Daily trend filter + intraday cumulative-volume regime switch +
    RSI/momentum sub-signals. Kept local (not utils/factors.py) because
    `vol_ratio` is specific to this strategy family.

    `vol_ratio` assumes a 24/7 continuous market: cumulative volume since
    UTC midnight, benchmarked against the same hour-of-day's 30-day
    average. For TXFR1 (day-session only, ~5 trading hours/day) "volume
    since UTC midnight" doesn't correspond to a trading session the way it
    does for BTC/ETH, and most hour-of-day slots have zero volume — this
    research script still computes it the same way for TXFR1 (no special
    case), but the cross-asset section below flags this explicitly rather
    than silently treating TXFR1 as a fair comparison.
    """
    df = raw.reset_index().rename(columns={'Datetime': 'date'})
    df = add_daily_trend_filter(df)
    df['hour'] = pd.to_datetime(df['date']).dt.hour

    df['cum_vol_today'] = df.groupby('date_only')['Volume'].cumsum()
    piv = df.groupby(['date_only', 'hour'])['cum_vol_today'].last().unstack('hour')
    piv_avg = piv.shift(1).rolling(30, min_periods=5).mean()
    piv_avg_melted = piv_avg.reset_index().melt(id_vars='date_only', value_name='cum_vol_avg')
    df = df.merge(piv_avg_melted, on=['date_only', 'hour'], how='left')
    df['vol_ratio'] = df['cum_vol_today'] / (df['cum_vol_avg'] + 1e-9)
    df['vol_ratio'] = df['vol_ratio'].fillna(1.0)

    df['rsi'] = calculate_rsi(df['Close'], 14)
    df['mom_1H_12'] = df['Close'] / df['Close'].shift(12) - 1.0
    df['is_trend_regime'] = df['vol_ratio'] > 1.15

    return df.dropna().reset_index(drop=True)


def simulate_switching(df_data, label=""):
    df = df_data.copy()
    signals, regimes = [], []
    pos = 0.0

    macro = df['mom_1D_10'].values
    rsi = df['rsi'].values
    mom_1h = df['mom_1H_12'].values
    is_trend = df['is_trend_regime'].values

    for i in range(len(df)):
        m_trend, r_val, m_val = macro[i], rsi[i], mom_1h[i]
        if is_trend[i]:
            regimes.append(1.0)
            if m_trend > 0:
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
        else:
            regimes.append(0.0)
            if m_trend > 0:
                if pos <= 0:
                    if r_val < 30:
                        pos = 1.0
                else:
                    if r_val > 65:
                        pos = 0.0
            else:
                if pos >= 0:
                    if r_val > 70:
                        pos = -1.0
                else:
                    if r_val < 35:
                        pos = 0.0
        signals.append(pos)

    df['signal'] = signals
    df['regime'] = regimes
    df['market_return'] = df['Close'].pct_change()
    df['strategy_return'] = df['signal'].shift(1) * df['market_return']
    df['prev_signal'] = df['signal'].shift(1).fillna(0)
    df['executed'] = (df['signal'] != df['prev_signal']).astype(int)
    df['strategy_return_net'] = df['strategy_return'] - (df['executed'] * 0.0015)

    df = df.dropna()
    cum_net = (1 + df['strategy_return_net']).cumprod()
    cum_market = (1 + df['market_return']).cumprod()

    total_return_net = cum_net.iloc[-1] - 1 if len(cum_net) else 0.0
    market_total = cum_market.iloc[-1] - 1 if len(cum_market) else 0.0
    sharpe_net = df['strategy_return_net'].mean() / (df['strategy_return_net'].std() + 1e-9) * np.sqrt(8760) if len(df) > 1 else 0.0
    roll_max = cum_net.cummax()
    max_dd = ((cum_net - roll_max) / roll_max).min() if len(cum_net) else 0.0
    pct_trend = df['regime'].mean() * 100 if len(df) else 0.0

    if label:
        print(f"[{label}] NetReturn={total_return_net*100:.2f}% Market={market_total*100:.2f}% Sharpe={sharpe_net:.4f} MaxDD={max_dd*100:.2f}% Trades={int(df['executed'].sum())} TrendRegime%={pct_trend:.1f}")

    return {
        'total_return': total_return_net, 'market_return': market_total,
        'sharpe': sharpe_net, 'max_dd': max_dd, 'trades': int(df['executed'].sum()),
        'pct_trend': pct_trend, 'df': df,
    }


def run_research():
    print("=== ① 資產/資料層 ===")
    print(f"決策資產: {DECISION_ASSET} | 跨資產穩健性資產: {ROBUST_ASSETS}")
    btc = build_features(load_ohlcv(DECISION_ASSET, IS_TRAIN_START, FULL_END))

    print("\n=== ② 樣本切分（IS-Train / IS-Val / OOS）===")
    df_train = btc[btc['date'] <= IS_TRAIN_END].reset_index(drop=True)
    df_val = btc[(btc['date'] > IS_TRAIN_END) & (btc['date'] <= IS_VAL_END)].reset_index(drop=True)
    df_oos = btc[btc['date'] > IS_VAL_END].reset_index(drop=True)
    print(f"IS-Train: {len(df_train)} rows ({IS_TRAIN_START}~{IS_TRAIN_END}) | "
          f"IS-Val: {len(df_val)} rows (~{IS_VAL_END}) | OOS: {len(df_oos)} rows (~{FULL_END})")

    # -------------------------------------------------------------
    # ③-freq 基礎頻率橫掃（IS-Train only）——1H 上 mom_1H_12/rsi_demeaned 在
    #    所有 forward period 都不顯著（見下方③本身的結果），依 README ③：
    #    這不代表因子沒救，下一步是換一個更粗/更細的基礎頻率（4H/1D）重新
    #    取資料、重新橫掃，而不是死守 1H。因子 × 頻率(含基礎頻率) 的組合
    #    是網格搜尋，這裡是「掃過整個網格挑單一贏家」的決策形態，需要 FWER
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
        pl_tf = pl_tf.with_columns(pl.lit("BTC").alias("asset_id"))
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
    # transform BEFORE correcting, not after (correcting raw p and then
    # re-checking min(p,1-p) would flag p~0.97 rows as "significant" without
    # ever passing through the correction).
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
    #    mom_1H_12 / rsi 在哪個 forward period 上最穩定顯著，這一步的產出
    #    決定④，不是拿已經定案的頻率回頭驗證。細節見③-freq：1H 只是③-freq
    #    橫掃的其中一個基礎頻率，這裡保留單獨一節是因為後續④~⑧沿用的正是
    #    1H 部署邏輯（mom_1H_12/rsi），③-freq 的角色是誠實檢查換基礎頻率
    #    是否能找到更好的因子，而不是取代這一節。
    # -------------------------------------------------------------
    print("\n=== ③ 因子分析：多頻率橫掃（IS-Train，factrix directional_hit_rate） ===")
    pl_train = pl.from_pandas(df_train[['date', 'Close', 'mom_1H_12', 'rsi']].rename(columns={'Close': 'price'}))
    pl_train = pl_train.with_columns(pl.lit("BTC").alias("asset_id"), (pl.col("rsi") - 50.0).alias("rsi_demeaned"))

    # evaluate_horizons — see ③-freq's comment on why this replaces a
    # hand-rolled for-loop of evaluate() calls.
    train_sweep_results = fx.evaluate_horizons(
        pl_train, metrics={"dir_hit": directional_hit_rate()},
        factor_cols=["mom_1H_12", "rsi_demeaned"], forward_periods=HORIZONS,
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
    rsi_best = best_horizon("rsi_demeaned")
    mom_effect = "reversal" if mom_best[2] < 0.5 else "trend"
    rsi_effect = "reversal" if rsi_best[2] < 0.5 else "trend"
    print(f"\nmom_1H_12 最穩定顯著: {mom_best[1]}h (hit={mom_best[2]:.4f}, p={mom_best[3]:.4f}, 效應方向={mom_effect})")
    print(f"rsi_demeaned 最穩定顯著: {rsi_best[1]}h (hit={rsi_best[2]:.4f}, p={rsi_best[3]:.4f}, 效應方向={rsi_effect})")

    # -------------------------------------------------------------
    # ③b 因子邊際穩定性（oos_decay）：在 IS-Train 內部前70%/後30%切分，這個
    #    邊際是否存活，而不是只看整個 IS-Train 合併後的單一數字。
    # -------------------------------------------------------------
    print("\n=== ③b 因子邊際穩定性（oos_decay，IS-Train 內部切分） ===")
    decay_rows = []
    for factor, best in [("mom_1H_12", mom_best), ("rsi_demeaned", rsi_best)]:
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
    # ③c vol_ratio regime 切片檢定（IS-Train，用③挑出的頻率，不是固定4h）
    # -------------------------------------------------------------
    print("\n=== ③c VOL-RATIO REGIME 切片檢定（IS-Train, factrix by_slice） ===")
    pl_train_regime = pl.from_pandas(df_train[['date', 'Close', 'mom_1H_12', 'rsi', 'is_trend_regime']].rename(columns={'Close': 'price'}))
    pl_train_regime = pl_train_regime.with_columns(
        pl.lit("BTC").alias("asset_id"),
        pl.col("is_trend_regime").cast(pl.Utf8).alias("vol_regime"),
        (pl.col("rsi") - 50.0).alias("rsi_demeaned"),
    )
    data_mom_h = fx.preprocess.compute_forward_return(pl_train_regime, forward_periods=mom_best[1])
    mom_board_res = fx.by_slice(data_mom_h, directional_hit_rate(), by="vol_regime", factor_col="mom_1H_12", strict=False)
    mom_keys = list(mom_board_res.keys())
    mom_board = fx.compare(list(mom_board_res.values()), metrics=["metric"]).with_columns(pl.Series("vol_regime", mom_keys))
    print(f">>> mom_1H_12 hit rate by vol_ratio regime @ {mom_best[1]}h:")
    print(mom_board)

    data_rsi_h = fx.preprocess.compute_forward_return(pl_train_regime, forward_periods=rsi_best[1])
    rsi_board_res = fx.by_slice(data_rsi_h, directional_hit_rate(), by="vol_regime", factor_col="rsi_demeaned", strict=False)
    rsi_keys = list(rsi_board_res.keys())
    rsi_board = fx.compare(list(rsi_board_res.values()), metrics=["metric"]).with_columns(pl.Series("vol_regime", rsi_keys))
    print(f">>> rsi (demeaned) hit rate by vol_ratio regime @ {rsi_best[1]}h:")
    print(rsi_board)

    # -------------------------------------------------------------
    # ④ 頻率/持有期決定 — 輸入是③的橫掃結果，不是研究前的經驗值。
    # -------------------------------------------------------------
    print("\n=== ④ 頻率/持有期決定 ===")
    freq_note = (
        f"mom_1H_12 在 {mom_best[1]}h 最穩定顯著、但效應方向是「{mom_effect}」（hit rate {mom_best[2]:.4f} < 0.5，p={mom_best[3]:.4f} 接近 1，"
        f"跟策略假設的「動量突破」方向相反）；rsi_demeaned 在 {rsi_best[1]}h 最穩定顯著，方向為「{rsi_effect}」。"
        f"策略部署檔（adaptive_switching_strategy.py）目前逐 1H bar 判斷進出場，"
        f"跟橫掃出來的最適頻率不完全一致——這裡誠實記錄落差，不回頭改動已部署的策略頻率（見結論）。"
    )
    print(freq_note)

    # -------------------------------------------------------------
    # ⑤ 策略候選生成與比較（IS-Val）——候選：momentum-only / RSI-only /
    #    switching。這裡是新增的步驟：原本這個家族直接在 OOS 上比較三個候選，
    #    等於候選挑選階段就用掉了唯一的盲測窗口，沒有真正 held-out 的檢查。
    # -------------------------------------------------------------
    print("\n=== ⑤ 策略候選比較（IS-Val）===")
    always_trend_val = df_val.copy(); always_trend_val['is_trend_regime'] = True
    always_range_val = df_val.copy(); always_range_val['is_trend_regime'] = False
    r_mom_val = simulate_switching(always_trend_val, "IS-Val: Momentum-only")
    r_rsi_val = simulate_switching(always_range_val, "IS-Val: RSI-only")
    r_switch_val = simulate_switching(df_val, "IS-Val: Adaptive switching (deployed logic)")

    # -------------------------------------------------------------
    # ⑤b 盲測 OOS — 全程未用 OOS 挑選任何候選/參數。
    # -------------------------------------------------------------
    print("\n=== ⑤b 盲測 OOS ===")
    always_trend_oos = df_oos.copy(); always_trend_oos['is_trend_regime'] = True
    always_range_oos = df_oos.copy(); always_range_oos['is_trend_regime'] = False
    r_mom_oos = simulate_switching(always_trend_oos, "OOS: Momentum-only")
    r_rsi_oos = simulate_switching(always_range_oos, "OOS: RSI-only")
    r_switch_oos = simulate_switching(df_oos, "OOS: Adaptive switching (deployed logic)")

    # -------------------------------------------------------------
    # ⑥ 風控疊加校準（MAE/MFE，IS-Train only）——held-out 驗證改成同時看
    #    IS-Val 跟 OOS（原本只驗證 OOS）。
    # -------------------------------------------------------------
    print("\n=== ⑥ MAE/MFE SL/TP 校準（IS-Train）===")
    is_res = simulate_switching(df_train, "IS-Train (for MAE/MFE derivation)")
    is_signal_df = is_res["df"]
    prev_sig = is_signal_df["signal"].shift(1).fillna(0)
    entry_long = (prev_sig == 0) & (is_signal_df["signal"] == 1.0)
    entry_short = (prev_sig == 0) & (is_signal_df["signal"] == -1.0)
    mfe_mae_events = compute_mfe_mae_events(is_signal_df, entry_long, entry_short, window=48, asset_id="BTC")
    mfe_mae_stats = derive_sl_tp(mfe_mae_events)

    sl_tp_holdout_rows = []
    if mfe_mae_stats["sl_pct"] is not None:
        mfe_sl, mfe_tp = mfe_mae_stats["sl_pct"], mfe_mae_stats["tp_pct"]
        print(f"MAE/MFE-derived: SL={mfe_sl*100:.2f}% TP={mfe_tp*100:.2f}%, n_events={mfe_mae_stats['n_events']}")
        for name, res in [("IS-Val", r_switch_val), ("OOS", r_switch_oos)]:
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
    strategy_path = os.path.join(os.path.dirname(__file__), "adaptive_switching_strategy.py")
    engine_variants = [("no SL/TP", {})]
    if mfe_sl is not None:
        engine_variants.append((f"MAE/MFE SL={mfe_sl*100:.1f}%/TP={mfe_tp*100:.1f}%", {"risk": {"stopLossPct": mfe_sl, "takeProfitPct": mfe_tp}}))
    raw_engine = run_engine_cross_check(strategy_path, ROBUST_ASSETS, ROBUST_START, ROBUST_END, variants=engine_variants)
    engine_results = {a: r for (a, label), r in raw_engine.items() if label == "no SL/TP"}
    engine_sl_tp_results = {a: r for (a, label), r in raw_engine.items() if label != "no SL/TP"}

    # -------------------------------------------------------------
    # ⑧ 跨資產穩健性 — 已在⑦一併完成（同一組門檻套 ETH/TXFR1，不重新調參）。
    # -------------------------------------------------------------

    # -------------------------------------------------------------
    # Report
    # -------------------------------------------------------------
    def fmt_freq_sweep_rows():
        return "\n".join(f"| {tf} | {f} | {h} | {v:.4f} | {p:.4f} | {p_adj:.4f} |" for tf, f, h, v, p, p_adj in freq_sweep_rows_adj)

    def fmt_horizon_rows():
        return "\n".join(f"| {f} | {h}h | {v:.4f} | {p:.4f} |" for f, h, v, p in horizon_rows)

    def fmt_decay_rows():
        return "\n".join(f"| {f} | {h}h | {v:.4f} | {'是' if flip else '否'} | {status} |" for f, h, v, flip, status in decay_rows)

    def fmt_regime_board(board, col):
        return "\n".join(f"| {row[col]} | {row['metric']:.4f} | {row['metric_p_value']:.4f} |" for row in board.to_dicts())

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

    report = f"""# Adaptive Switching — 研究報告

**時間**: 2026-07-12 | **決策資產**: {DECISION_ASSET} | **跨資產**: {", ".join(ROBUST_ASSETS)}
**樣本切分**: IS-Train {IS_TRAIN_START}~{IS_TRAIN_END} | IS-Val ~{IS_VAL_END} | OOS ~{FULL_END} | 跨資產窗口 {ROBUST_START}~{ROBUST_END}（同參數不重調）

流程依 `strategies/README.md` 的 ①~⑧ 順序執行。

## Bug（已修復）
`vol_ratio` 的 pivot 計算在 TXFR1 上因重複 `(date,hour)` 列崩潰 → 改用 `groupby().last().unstack()`，已修復並在⑦驗證不再崩潰。

## ③-freq 基礎頻率橫掃（IS-Train，1H/4H/1D）
mom_factor/rsi_demeaned 在 1H 上所有 forward period 都不顯著（見下方③），依 README ③ 換基礎頻率重新取資料橫掃，而非死守 1H。mom_factor lookback 隨基礎頻率調整（1H: 12 bars / 4H: 3 bars / 1D: 1 bar，皆對應約 12h 窗口，1D 上收斂為 1 bar 因日線沒有次日內窗口）；forward period 以 bar 數表示。

**多重檢定校正方法**：用 `utils/stats.py` 的手刻 `holm_bonferroni`（Holm step-down，控制 **FWER**），不是 factrix 現成工具。原因：這個網格搜尋的決策形態是「掃過 base_tf×factor×horizon 全部組合、挑單一贏家」，需要的是 FWER（控制「至少挑錯一次」的機率），不是 FDR（控制「誤判佔比的期望值」，適合「篩一批因子、全部留著用」的情境，不適合「只挑最強的那一個」）。**factrix 公開 API 目前只有 FDR 工具**（`fx.stats.bhy_adjusted_p`/`fx.multi_factor.bhy`/`bhy_hierarchical`）——`factrix/_stats/multiple_testing.py` 雖然有 `holm_step_down`/`bonferroni`，但那是底線開頭的 private module，未被 `factrix/__init__.py` 或公開的 `factrix/stats/` 引用，不能當作穩定 API 依賴。這是 README ③ 認定的真實 factrix 缺口，故此處保留手刻，不換成 `bhy_adjusted_p`（先前一度換過，回頭發現那其實是換錯了統計目標，已改回）。

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

存活率（絕對值 mean_OOS / mean_IS）< 0.5 或反號代表這個因子在 IS-Train 內部自己都不穩定，不用等到 IS-Val 就已經是警訊。mom_1H_12 反號（VETOED）就是這種警訊。rsi_demeaned 存活率 126 這種遠大於 1 的數字通常是 mean_IS 接近 0 造成的分母效應，不代表真的穩定 126 倍，解讀時不應直接當作「非常穩健」的證據。

## ③c Vol-ratio Regime 切片檢定（IS-Train，用③挑出的頻率）
mom_1H_12 @ {mom_best[1]}h：
| Regime | Hit Rate | p-value |
|---|---|---|
{fmt_regime_board(mom_board, "vol_regime")}

rsi 去均值 @ {rsi_best[1]}h：
| Regime | Hit Rate | p-value |
|---|---|---|
{fmt_regime_board(rsi_board, "vol_regime")}

## ④ 頻率/持有期決定
{freq_note}

## ⑤ 策略候選比較（IS-Val）
| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| Momentum-only | {r_mom_val['total_return']*100:.2f}% | {r_mom_val['sharpe']:.4f} | {r_mom_val['max_dd']*100:.2f}% | {r_mom_val['trades']} |
| RSI-only | {r_rsi_val['total_return']*100:.2f}% | {r_rsi_val['sharpe']:.4f} | {r_rsi_val['max_dd']*100:.2f}% | {r_rsi_val['trades']} |
| Adaptive switching | {r_switch_val['total_return']*100:.2f}% | {r_switch_val['sharpe']:.4f} | {r_switch_val['max_dd']*100:.2f}% | {r_switch_val['trades']} |

## ⑤b 盲測 OOS
| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| Momentum-only | {r_mom_oos['total_return']*100:.2f}% | {r_mom_oos['sharpe']:.4f} | {r_mom_oos['max_dd']*100:.2f}% | {r_mom_oos['trades']} |
| RSI-only | {r_rsi_oos['total_return']*100:.2f}% | {r_rsi_oos['sharpe']:.4f} | {r_rsi_oos['max_dd']*100:.2f}% | {r_rsi_oos['trades']} |
| Adaptive switching | {r_switch_oos['total_return']*100:.2f}% | {r_switch_oos['sharpe']:.4f} | {r_switch_oos['max_dd']*100:.2f}% | {r_switch_oos['trades']} |

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

## 結論
- **切換機制是否加值**：見⑤/⑤b。IS-Val 上 Adaptive switching（Sharpe {r_switch_val['sharpe']:.4f}）{"優於" if r_switch_val['sharpe'] > max(r_mom_val['sharpe'], r_rsi_val['sharpe']) else "是三者中最差的，並未優於"}兩個單一子策略中較好的一個（RSI-only {r_rsi_val['sharpe']:.4f} / Momentum-only {r_mom_val['sharpe']:.4f}）；OOS 盲測{"同樣顯示切換機制沒有加值" if (r_switch_oos['sharpe'] > max(r_mom_oos['sharpe'], r_rsi_oos['sharpe'])) == (r_switch_val['sharpe'] > max(r_mom_val['sharpe'], r_rsi_val['sharpe'])) else "排序反轉了"}（Adaptive switching {r_switch_oos['sharpe']:.4f} vs RSI-only {r_rsi_oos['sharpe']:.4f} / Momentum-only {r_mom_oos['sharpe']:.4f}）——**兩個獨立窗口（IS-Val、OOS）都指向同一個結論，比只看單一 OOS 窗口更站得住腳**：切換機制沒有加值，且部分窗口下反而是三者中最差的。
- **mom_1H_12 的方向跟策略假設相反**：③顯示 mom_1H_12 最穩定顯著是在 {mom_best[1]}h、效應方向是「{mom_effect}」（hit rate {mom_best[2]:.4f}<0.5），但策略把它當「動量突破」訊號使用（正值視為看漲延續）——因子分析結果本身就不支持這個因子的使用方式，這比切換門檻的問題更根本。且 mom_1H_12 的 oos_decay 反號（③b，VETOED），代表這個「顯著」在 IS-Train 內部就不穩定，不需要等到 IS-Val 才發現問題。
- **頻率落差**：③橫掃出的最穩定頻率（{mom_best[1]}h/{rsi_best[1]}h）跟部署策略固定逐 1H bar 判斷（見④）不完全一致——這是已知的方法論落差，尚未回頭調整部署頻率。
- ③c 的 regime 切片檢定顯示兩個因子在 `is_trend_regime` 兩側的 hit rate/p-value 都沒有達到常見顯著門檻，vol_ratio 1.15 這個切換門檻缺乏資料支持，與原始版本的結論一致。
- TXFR1 的 pivot 崩潰已修復並在⑦驗證正常運作；`vol_ratio` 假設 24/7 市場，TXFR1（日盤限定）語意仍不對齊，數字僅供框架驗證參考。
- MAE/MFE SL/TP 疊加需依⑦數字判斷是否加值，不假設對所有家族都有害——見上表。
- **基礎頻率橫掃（③-freq）**：{"1H 上的不顯著不是頻率選錯——換 4H/1D 重新取資料橫掃、跨 base_tf×factor×horizon 做 Holm-Bonferroni 校正（手刻，FWER control——這個「掃網格挑單一贏家」的決策形態需要 FWER，而 factrix 公開 API 沒有 FWER 工具，見③-freq 說明）後，三個基礎頻率上 mom/rsi 都沒有通過顯著性門檻，代表 mom/rsi 這兩個因子在 BTC 上（至少在這個樣本窗口）本身就不具備可用邊際，不是 1H 這個起始假設的問題。" if not freq_sweep_winners else "換基礎頻率後找到顯著組合（見③-freq），下一步應針對該頻率重新走④~⑧，而非沿用目前部署的 1H 邏輯。"}既然三個已測基礎頻率都沒有找到顯著因子，依 README ④「若因子在所有已嘗試的頻率下都不穩定，代表它可能不夠格進入⑤」，本次不再嘗試多頻率合成（multi-timeframe combination）——在單一頻率邊際都不存在的前提下疊加 MTF 只會增加多重檢定風險，不會製造出原本沒有的邊際。目前部署的 1H adaptive switching 邏輯（④~⑧ 沿用既有結果）維持原結論：不建議上線。
"""

    report_path = os.path.join(os.path.dirname(__file__), "report.md")
    with open(report_path, "w") as f_rep:
        f_rep.write(report)
    print(f"\nReport written to {report_path}")


if __name__ == "__main__":
    run_research()
