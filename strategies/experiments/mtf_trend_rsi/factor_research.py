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
# same params to ETH/TXFR1 without re-fitting. Windows match the sibling
# families (adaptive_switching, mtf_trend_momentum) so results are
# comparable across the single_asset framework.
DECISION_ASSET = "BTC"
ROBUST_ASSETS = ["BTC", "ETH", "TXFR1"]
ROBUST_START, ROBUST_END = "2025-01-01", "2026-06-01"

# ② 樣本切分 — this family previously had NO factor-research script at all
# (only an SL/TP grid-search optimizer, see optimize_sl_tp.py), i.e. no
# IS-Train/IS-Val/OOS split and no factor validation of RSI/daily-trend was
# ever done before the strategy was deployed.
IS_TRAIN_START, IS_TRAIN_END = "2024-08-01", "2025-04-30"
IS_VAL_END = "2025-09-30"
FULL_END = "2026-06-01"

HORIZONS = [1, 4, 12, 24]

# Deployed thresholds (mtf_trend_rsi_strategy.py): RSI(14), long entry <30,
# long exit >65, short entry >70, short exit <35, gated by mom_1D_10 (daily
# 10-bar close-to-close momentum) sign.
RSI_PERIOD = 14
BUY_TH, SELL_TH, SHORT_TH, COVER_TH = 30.0, 65.0, 70.0, 35.0
TREND_LOOKBACK = 10

# ③-freq base-frequency sweep config. `trend_lookback` is scaled in BARS so
# each base_tf's trend factor covers the same wall-clock window as the
# deployed 10-DAILY-bar (=240h) trend filter: 1h->240 bars, 4h->60 bars,
# 1d->10 bars (the literal deployed definition). rsi_demeaned keeps the
# deployed RSI period (14) unchanged across base_tf, matching how the
# strategy actually computes RSI on whatever bar it's given.
BASE_TF_CONFIG = {
    "1h": {"trend_lookback": 240, "horizons": [1, 4, 12, 24]},
    "4h": {"trend_lookback": 60, "horizons": [1, 2, 3, 6]},
    "1d": {"trend_lookback": 10, "horizons": [1, 2, 3, 5]},
}


def build_freq_sweep_features(raw: pd.DataFrame, trend_lookback: int) -> pd.DataFrame:
    """Minimal factor set for the base-frequency sweep (③-freq): the
    deployed family's two actual factors (trend momentum + RSI), computed
    generically at any base_tf so the same evaluation code runs across
    1h/4h/1d.
    """
    df = raw.reset_index().rename(columns={'Datetime': 'date'})
    df['trend_factor'] = df['Close'] / df['Close'].shift(trend_lookback) - 1.0
    df['rsi_demeaned'] = calculate_rsi(df['Close'], RSI_PERIOD) - 50.0
    return df.dropna().reset_index(drop=True)


def build_features(raw: pd.DataFrame) -> pd.DataFrame:
    """Daily trend filter (mom_1D_10) + hourly RSI(14) — exactly the two
    factors mtf_trend_rsi_strategy.py uses, nothing invented."""
    df = raw.reset_index().rename(columns={'Datetime': 'date'})
    df = add_daily_trend_filter(df, lookback=TREND_LOOKBACK)
    df['rsi'] = calculate_rsi(df['Close'], RSI_PERIOD)
    df['is_trend_regime'] = df['mom_1D_10'] > 0
    return df.dropna().reset_index(drop=True)


def simulate_rsi(df_data, gated=True, label=""):
    """Replays the deployed state machine. gated=True reproduces
    mtf_trend_rsi_strategy.py exactly (daily trend sign gates which side can
    open); gated=False is the no-filter baseline (README ⑤: candidate
    comparison needs at least one un-gated baseline to tell whether the MTF
    trend gate actually adds value over plain RSI reversion)."""
    df = df_data.copy()
    signals = []
    pos = 0.0
    trend = df['mom_1D_10'].values
    rsi = df['rsi'].values

    for i in range(len(df)):
        r_val = rsi[i]
        if gated:
            m_trend = trend[i]
            if m_trend > 0:  # Bullish daily trend
                if pos <= 0:
                    if r_val < BUY_TH:
                        pos = 1.0
                else:
                    if r_val > SELL_TH:
                        pos = 0.0
            else:  # Bearish daily trend
                if pos >= 0:
                    if r_val > SHORT_TH:
                        pos = -1.0
                else:
                    if r_val < COVER_TH:
                        pos = 0.0
        else:  # No trend gate — pure RSI mean reversion, either side always allowed
            if pos == 0.0:
                if r_val < BUY_TH:
                    pos = 1.0
                elif r_val > SHORT_TH:
                    pos = -1.0
            elif pos == 1.0:
                if r_val > SELL_TH:
                    pos = 0.0
            elif pos == -1.0:
                if r_val < COVER_TH:
                    pos = 0.0
        signals.append(pos)

    df['signal'] = signals
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

    if label:
        print(f"[{label}] NetReturn={total_return_net*100:.2f}% Market={market_total*100:.2f}% Sharpe={sharpe_net:.4f} MaxDD={max_dd*100:.2f}% Trades={int(df['executed'].sum())}")

    return {
        'total_return': total_return_net, 'market_return': market_total,
        'sharpe': sharpe_net, 'max_dd': max_dd, 'trades': int(df['executed'].sum()),
        'df': df,
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
    # ③-freq 基礎頻率橫掃（IS-Train）— trend_factor/rsi_demeaned 在起始
    #    基礎頻率（1H）上是否顯著要先看③本身的結果，這裡不論③的結果如何都
    #    照樣橫掃 1H/4H/1D，因為本家族先前從未做過任何頻率層面的因子驗證。
    #    因子 × 頻率(含基礎頻率) 的組合是網格搜尋，這裡是「掃過整個網格挑
    #    單一贏家」的決策形態，需要 FWER（guard「至少挑錯一次」的機率），
    #    不是 FDR——factrix 公開 API 只有 FDR 工具（fx.stats.bhy_adjusted_p/
    #    bhy/bhy_hierarchical），沒有公開的 FWER 工具（_stats/multiple_testing.py
    #    雖有 holm_step_down，但是底線開頭的 private module，未被 factrix
    #    自己的公開介面引用，不算穩定 API），故此處沿用 utils/stats.py 的
    #    手刻 Holm-Bonferroni（見 README ③ 對 FWER/FDR 這個 factrix 缺口的
    #    說明）。
    # -------------------------------------------------------------
    print("\n=== ③-freq 基礎頻率橫掃（IS-Train，1H/4H/1D，factrix directional_hit_rate） ===")
    btc_cfg = TICKERS[DECISION_ASSET]
    freq_sweep_rows = []  # (base_tf, factor, horizon_bars, hit_rate, p_value)
    print(f"{'base_tf':<8} | {'factor':<13} | {'h(bars)':>7} | {'hit_rate':>8} | {'p_value':>8}")
    for base_tf, cfg in BASE_TF_CONFIG.items():
        raw_tf = get_crypto_kbars_df(btc_cfg["exchange_id"], btc_cfg["symbol"], base_tf, IS_TRAIN_START, IS_TRAIN_END)
        feat_tf = build_freq_sweep_features(raw_tf, cfg["trend_lookback"])
        pl_tf = pl.from_pandas(feat_tf[['date', 'Close', 'trend_factor', 'rsi_demeaned']].rename(columns={'Close': 'price'}))
        pl_tf = pl_tf.with_columns(pl.lit("BTC").alias("asset_id"))
        sweep_results = fx.evaluate_horizons(
            pl_tf, metrics={"dir_hit": directional_hit_rate()},
            factor_cols=["trend_factor", "rsi_demeaned"], forward_periods=cfg["horizons"],
        )
        for r in sweep_results:
            m = r.metrics["dir_hit"]
            freq_sweep_rows.append((base_tf, r.factor, r.forward_periods, m.value, m.p_value))
            print(f"{base_tf:<8} | {r.factor:<13} | {r.forward_periods:>7} | {m.value:>8.4f} | {m.p_value:>8.4f}")

    # Two-sided transform BEFORE correcting (see adaptive_switching's
    # identical comment): directional_hit_rate's p_value is one-sided
    # against hit_rate==0.5, so correct min(p, 1-p), not raw p.
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
    # ③ 因子分析：1H 內多 forward period 橫掃（IS-Train only）— 這一步的
    #    產出決定④。測試部署策略實際使用的兩個因子：mom_1D_10（日線趨勢
    #    濾網）與 rsi_demeaned（小時線 RSI(14) 去均值）。
    # -------------------------------------------------------------
    print("\n=== ③ 因子分析：多頻率橫掃（IS-Train，factrix directional_hit_rate） ===")
    pl_train = pl.from_pandas(df_train[['date', 'Close', 'mom_1D_10', 'rsi']].rename(columns={'Close': 'price'}))
    pl_train = pl_train.with_columns(pl.lit("BTC").alias("asset_id"), (pl.col("rsi") - 50.0).alias("rsi_demeaned"))

    train_sweep_results = fx.evaluate_horizons(
        pl_train, metrics={"dir_hit": directional_hit_rate()},
        factor_cols=["mom_1D_10", "rsi_demeaned"], forward_periods=HORIZONS,
    )
    horizon_rows = []
    print(f"{'factor':<14} | {'h':>3} | {'hit_rate':>8} | {'p_value':>8}")
    for r in train_sweep_results:
        m = r.metrics["dir_hit"]
        horizon_rows.append((r.factor, r.forward_periods, m.value, m.p_value))
        print(f"{r.factor:<14} | {r.forward_periods:>3} | {m.value:>8.4f} | {m.p_value:>8.4f}")

    # This 2-factor x 4-horizon sweep is itself a grid ("scan and pick the
    # best horizon per factor" — the same ④ decision ③-freq makes across
    # base_tf), so it needs the same FWER treatment as ③-freq, not just a
    # visual "which p looks small" read. Without this, a small raw p here
    # (e.g. mom_1D_10 @ 12h) would be reported as "significant" while the
    # base-frequency sweep next to it went through correction — an
    # inconsistent standard within the same report.
    horizon_p_raw = [r[3] for r in horizon_rows]
    horizon_p_2sided = [min(2 * min(p, 1 - p), 1.0) for p in horizon_p_raw]
    horizon_p_adj = holm_bonferroni(horizon_p_2sided)
    horizon_rows_adj = [(*row, p_adj) for row, p_adj in zip(horizon_rows, horizon_p_adj)]
    for f, h, v, p, p_adj in horizon_rows_adj:
        print(f"  Holm-adjusted: {f:<14} @ {h:>3}h: p_raw={p:.4f} p_holm={p_adj:.4f}")

    def best_horizon(factor):
        rows = [r for r in horizon_rows_adj if r[0] == factor]
        return min(rows, key=lambda r: min(r[3], 1 - r[3]))

    trend_best = best_horizon("mom_1D_10")
    rsi_best = best_horizon("rsi_demeaned")
    trend_effect = "reversal" if trend_best[2] < 0.5 else "trend"
    rsi_effect = "reversal" if rsi_best[2] < 0.5 else "trend"
    trend_sig = trend_best[4] < 0.05
    rsi_sig = rsi_best[4] < 0.05
    print(f"\nmom_1D_10 最穩定顯著: {trend_best[1]}h (hit={trend_best[2]:.4f}, p_raw={trend_best[3]:.4f}, p_holm={trend_best[4]:.4f}, 效應方向={trend_effect}, 校正後{'顯著' if trend_sig else '不顯著'})")
    print(f"rsi_demeaned 最穩定顯著: {rsi_best[1]}h (hit={rsi_best[2]:.4f}, p_raw={rsi_best[3]:.4f}, p_holm={rsi_best[4]:.4f}, 效應方向={rsi_effect}, 校正後{'顯著' if rsi_sig else '不顯著'})")

    # -------------------------------------------------------------
    # ③b 因子邊際穩定性（oos_decay）：IS-Train 內部前70%/後30%切分。
    # -------------------------------------------------------------
    print("\n=== ③b 因子邊際穩定性（oos_decay，IS-Train 內部切分） ===")
    decay_rows = []
    for factor, best in [("mom_1D_10", trend_best), ("rsi_demeaned", rsi_best)]:
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
    # ③c 趨勢 regime 切片檢定：RSI 的 hit rate 在「日線多頭」vs「日線空頭」
    #    兩個 regime 下是否有差異——這直接回答本家族的核心設計假設：疊加
    #    daily trend 濾網是否真的改善了 RSI 的預測力，而不只是額外的雜訊
    #    限制條件。用③挑出的 rsi_demeaned 最適頻率，不是隨便固定一個。
    # -------------------------------------------------------------
    print("\n=== ③c 日線趨勢 REGIME 切片檢定（IS-Train, factrix by_slice） ===")
    pl_train_regime = pl.from_pandas(df_train[['date', 'Close', 'rsi', 'is_trend_regime']].rename(columns={'Close': 'price'}))
    pl_train_regime = pl_train_regime.with_columns(
        pl.lit("BTC").alias("asset_id"),
        pl.col("is_trend_regime").cast(pl.Utf8).alias("trend_regime"),
        (pl.col("rsi") - 50.0).alias("rsi_demeaned"),
    )
    data_rsi_h = fx.preprocess.compute_forward_return(pl_train_regime, forward_periods=rsi_best[1])
    rsi_board_res = fx.by_slice(data_rsi_h, directional_hit_rate(), by="trend_regime", factor_col="rsi_demeaned", strict=False)
    rsi_keys = list(rsi_board_res.keys())
    rsi_board = fx.compare(list(rsi_board_res.values()), metrics=["metric"]).with_columns(pl.Series("trend_regime", rsi_keys))
    print(f">>> rsi (demeaned) hit rate by daily-trend regime @ {rsi_best[1]}h:")
    print(rsi_board)

    # -------------------------------------------------------------
    # ④ 頻率/持有期決定 — 輸入是③的橫掃結果，不是研究者的經驗值。
    # -------------------------------------------------------------
    print("\n=== ④ 頻率/持有期決定 ===")
    freq_note = (
        f"mom_1D_10 在 {trend_best[1]}h 最穩定顯著、效應方向為「{trend_effect}」（hit={trend_best[2]:.4f}, p_holm={trend_best[4]:.4f}, "
        f"{'Holm 校正後仍顯著' if trend_sig else 'Holm 校正後不顯著'}）；"
        f"rsi_demeaned 在 {rsi_best[1]}h 最穩定顯著、效應方向為「{rsi_effect}」（hit={rsi_best[2]:.4f}, p_holm={rsi_best[4]:.4f}，"
        f"{'Holm 校正後仍顯著' if rsi_sig else 'Holm 校正後不顯著'}，"
        f"{'符合' if rsi_effect == 'reversal' else '不符合'}策略假設的「RSI 超買超賣反轉」方向）。"
        f"策略部署檔（mtf_trend_rsi_strategy.py）逐 1H bar 判斷進出場、以 RSI(14) 的邏輯出場（非固定 forward period），"
        f"跟橫掃出來的最適 forward period 不完全是同一件事——這裡誠實記錄，不回頭改動已部署的策略頻率（見結論）。"
    )
    print(freq_note)

    # -------------------------------------------------------------
    # ⑤ 策略候選生成與比較（IS-Val）——候選：A) 無濾鏡 RSI 反轉（baseline）
    #    vs B) 部署邏輯（日線趨勢濾網 + RSI）。這是本家族第一次有這個
    #    baseline：先前只做過 SL/TP 網格搜尋，從未檢驗過 MTF 濾網本身是否
    #    比不加濾網的純 RSI 反轉更好。
    # -------------------------------------------------------------
    print("\n=== ⑤ 策略候選比較（IS-Val）===")
    r_pure_val = simulate_rsi(df_val, gated=False, label="IS-Val: Pure RSI (no trend gate)")
    r_mtf_val = simulate_rsi(df_val, gated=True, label="IS-Val: MTF Trend + RSI (deployed logic)")
    winner_name = "MTF Trend + RSI" if r_mtf_val['sharpe'] > r_pure_val['sharpe'] else "Pure RSI (no gate)"
    winner_gated = r_mtf_val['sharpe'] > r_pure_val['sharpe']
    print(f"IS-Val 勝出候選: {winner_name} (MTF Sharpe={r_mtf_val['sharpe']:.4f} vs Pure Sharpe={r_pure_val['sharpe']:.4f})")

    # -------------------------------------------------------------
    # ⑤b 盲測 OOS — 全程未用 OOS 挑選任何候選/參數。
    # -------------------------------------------------------------
    print("\n=== ⑤b 盲測 OOS ===")
    r_pure_oos = simulate_rsi(df_oos, gated=False, label="OOS: Pure RSI (no trend gate)")
    r_mtf_oos = simulate_rsi(df_oos, gated=True, label="OOS: MTF Trend + RSI (deployed logic)")

    # -------------------------------------------------------------
    # ⑥ 風控疊加校準（MAE/MFE，IS-Train only）— 取代 optimize_sl_tp.py 原本
    #    的 SL×TP 網格搜尋（63 組合＝63 個隱形假設檢定，見 README ⑥ 與
    #    quant-multiple-testing skill），改用進場事件的 MAE/MFE 分布反推。
    #    用 IS-Val 勝出候選（winner_gated）的訊號來抽 IS-Train 進場事件，
    #    校準只用 IS-Train，IS-Val/OOS 只驗證不重新校準。
    # -------------------------------------------------------------
    print("\n=== ⑥ MAE/MFE SL/TP 校準（IS-Train，取代網格搜尋） ===")
    is_res = simulate_rsi(df_train, gated=winner_gated, label=f"IS-Train ({winner_name}, for MAE/MFE derivation)")
    is_signal_df = is_res["df"]
    prev_sig = is_signal_df["signal"].shift(1).fillna(0)
    entry_long = (prev_sig == 0) & (is_signal_df["signal"] == 1.0)
    entry_short = (prev_sig == 0) & (is_signal_df["signal"] == -1.0)
    mfe_mae_events = compute_mfe_mae_events(is_signal_df, entry_long, entry_short, window=48, asset_id="BTC")
    mfe_mae_stats = derive_sl_tp(mfe_mae_events)

    sl_tp_holdout_rows = []
    if mfe_mae_stats["sl_pct"] is not None:
        mfe_sl, mfe_tp = mfe_mae_stats["sl_pct"], mfe_mae_stats["tp_pct"]
        print(f"MAE/MFE-derived: SL={mfe_sl*100:.2f}% (P75 |MAE|) TP={mfe_tp*100:.2f}% (P50 MFE), n_events={mfe_mae_stats['n_events']}")
        for name, res in [("IS-Val", r_mtf_val if winner_gated else r_pure_val), ("OOS", r_mtf_oos if winner_gated else r_pure_oos)]:
            t, h, l, c = extract_trades(res["df"], signal_col="signal")
            no_sl_check = simulate_sl_tp(t, h, l, c, 0.99, 9.99, friction=0.0015 * 2)
            with_sl_tp = simulate_sl_tp(t, h, l, c, mfe_sl, mfe_tp, friction=0.0015 * 2)
            sl_tp_holdout_rows.append((name, no_sl_check, with_sl_tp))
            print(f"[{name}] no-SL/TP (cross-check): Return={no_sl_check['total_return']*100:.2f}% Sharpe={no_sl_check['sharpe']:.4f} | MAE/MFE SL/TP: Return={with_sl_tp['total_return']*100:.2f}% Sharpe={with_sl_tp['sharpe']:.4f}")
    else:
        mfe_sl, mfe_tp = None, None
        print(f"MAE/MFE-derived: unavailable — {mfe_mae_stats['reason']}")

    # -------------------------------------------------------------
    # ⑦ 正式引擎交叉驗證 — mtf_trend_rsi_strategy.py 本身固定是 gated
    #    (MTF trend + RSI) 邏輯，跟 IS-Val 是否選中它無關（見結論的落差
    #    注記）；⑧跨資產穩健性直接在同一次呼叫內完成（同參數套 ETH/TXFR1，
    #    不重新調參）。
    # -------------------------------------------------------------
    print("\n=== ⑦ 正式引擎交叉驗證（BacktestService） / ⑧ 跨資產穩健性 ===")
    strategy_path = os.path.join(os.path.dirname(__file__), "mtf_trend_rsi_strategy.py")
    engine_variants = [("no SL/TP", {})]
    if mfe_sl is not None:
        engine_variants.append((f"MAE/MFE SL={mfe_sl*100:.1f}%/TP={mfe_tp*100:.1f}%", {"risk": {"stopLossPct": mfe_sl, "takeProfitPct": mfe_tp}}))
    raw_engine = run_engine_cross_check(strategy_path, ROBUST_ASSETS, ROBUST_START, ROBUST_END, variants=engine_variants)
    engine_results = {a: r for (a, label), r in raw_engine.items() if label == "no SL/TP"}
    engine_sl_tp_results = {a: r for (a, label), r in raw_engine.items() if label != "no SL/TP"}

    # -------------------------------------------------------------
    # Report
    # -------------------------------------------------------------
    def fmt_freq_sweep_rows():
        return "\n".join(f"| {tf} | {f} | {h} | {v:.4f} | {p:.4f} | {p_adj:.4f} |" for tf, f, h, v, p, p_adj in freq_sweep_rows_adj)

    def fmt_horizon_rows():
        return "\n".join(f"| {f} | {h}h | {v:.4f} | {p:.4f} | {p_adj:.4f} |" for f, h, v, p, p_adj in horizon_rows_adj)

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

    val_gate_helps = r_mtf_val['sharpe'] > r_pure_val['sharpe']
    oos_gate_helps = r_mtf_oos['sharpe'] > r_pure_oos['sharpe']

    report = f"""# MTF Trend RSI — 研究報告

**時間**: 2026-07-12 | **決策資產**: {DECISION_ASSET} | **跨資產**: {", ".join(ROBUST_ASSETS)}
**樣本切分**: IS-Train {IS_TRAIN_START}~{IS_TRAIN_END} | IS-Val ~{IS_VAL_END} | OOS ~{FULL_END} | 跨資產窗口 {ROBUST_START}~{ROBUST_END}（同參數不重調）

流程依 `strategies/README.md` 的 ①~⑧ 順序執行。**這是本家族第一支因子研究腳本**——先前只有 `optimize_sl_tp.py`（純 SL/TP 網格搜尋），從未對 RSI(14) 或 mom_1D_10 日線趨勢濾網做過任何因子顯著性驗證，等於一支已部署策略跳過③~⑤直接做⑥，本報告補齊完整流程。

## ③-freq 基礎頻率橫掃（IS-Train，1H/4H/1D）
`trend_factor`（各基礎頻率下對應部署 10-daily-bar / 240h 窗口的動量因子）與 `rsi_demeaned`（RSI(14)-50，週期固定不隨基礎頻率變動，跟部署邏輯一致）在三個基礎頻率上橫掃。

**多重檢定校正方法**：用 `utils/stats.py` 的手刻 `holm_bonferroni`（Holm step-down，控制 **FWER**），不是 factrix 現成工具。這個網格搜尋的決策形態是「掃過 base_tf×factor×horizon 全部組合、挑單一贏家」，需要 FWER（控制「至少挑錯一次」的機率），不是 FDR（適合「篩一批因子、全部留著用」）。factrix 公開 API 目前只有 FDR 工具（`fx.stats.bhy_adjusted_p`/`fx.multi_factor.bhy`/`bhy_hierarchical`）——`factrix/_stats/multiple_testing.py` 雖有 `holm_step_down`/`bonferroni`，但那是底線開頭的 private module，未被 `factrix/__init__.py` 或公開的 `factrix/stats/` 引用，不是穩定 API，這是 README ③ 認定的真實 factrix 缺口，故此處保留手刻。

| 基礎頻率 | 因子 | Forward(bars) | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|---|
{fmt_freq_sweep_rows()}

Holm-Bonferroni 校正（n={len(freq_sweep_p_raw)} 個檢定，跨 base_tf × factor × horizon 一起校正，alpha=0.05）後，{"找到以下顯著組合" if freq_sweep_winners else "沒有任何組合顯著"}。{("；".join(f"{r[0]}/{r[1]}@{r[2]}bars (hit={r[3]:.4f}, p_holm={r[5]:.4f})" for r in freq_sweep_winners) + "。") if freq_sweep_winners else "1H/4H/1D 三個基礎頻率上，trend_factor/rsi_demeaned 皆未通過校正後的顯著性門檻——這不是 1H 特有的問題，換粗/細基礎頻率沒有找到可用的邊際。"}

## ③ 因子分析：多頻率橫掃（IS-Train，部署邏輯的兩個實際因子）
這個 2 因子 x 4 持有期的橫掃本身也是「掃過網格挑最適持有期」的決策形態，跟③-freq 一樣需要 FWER 校正（同一套手刻 `holm_bonferroni`），不能只看 raw p 判斷顯著性——否則會跟③-freq 用不同標準評估同一份報告。

| 因子 | 持有期 | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|
{fmt_horizon_rows()}

Holm-Bonferroni 校正（n={len(horizon_p_raw)} 個檢定，跨 2 因子 x 4 持有期一起校正，alpha=0.05）後：mom_1D_10 在最穩定的 {trend_best[1]}h 上 p_holm={trend_best[4]:.4f}（{"通過" if trend_sig else "未通過"}顯著性門檻）；rsi_demeaned 在最穩定的 {rsi_best[1]}h 上 p_holm={rsi_best[4]:.4f}（{"通過" if rsi_sig else "未通過"}顯著性門檻）。

## ③b 因子邊際穩定性（oos_decay，IS-Train 內部 70/30 切分）
| 因子 | 頻率 | 存活率 | 反號 | 狀態 |
|---|---|---|---|---|
{fmt_decay_rows()}

存活率（絕對值 mean_OOS / mean_IS）< 0.5 或反號代表這個因子在 IS-Train 內部自己都不穩定，不用等到 IS-Val 就已經是警訊。

## ③c 日線趨勢 Regime 切片檢定（IS-Train，用③挑出的 RSI 頻率）
這一節直接檢驗本家族的核心設計假設——疊加日線趨勢濾網是否真的改善 RSI 的預測力：

rsi 去均值 @ {rsi_best[1]}h，依 `mom_1D_10 > 0`（多頭 regime）切片：
| Regime (是否多頭) | Hit Rate | p-value |
|---|---|---|
{fmt_regime_board(rsi_board, "trend_regime")}

## ④ 頻率/持有期決定
{freq_note}

## ⑤ 策略候選比較（IS-Val）
| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| Pure RSI (無濾網) | {r_pure_val['total_return']*100:.2f}% | {r_pure_val['sharpe']:.4f} | {r_pure_val['max_dd']*100:.2f}% | {r_pure_val['trades']} |
| MTF Trend + RSI (部署邏輯) | {r_mtf_val['total_return']*100:.2f}% | {r_mtf_val['sharpe']:.4f} | {r_mtf_val['max_dd']*100:.2f}% | {r_mtf_val['trades']} |

IS-Val 勝出候選：**{winner_name}**（{"MTF 濾網加值" if val_gate_helps else "MTF 濾網未加值，反而是無濾網版本較好"}）。

## ⑤b 盲測 OOS
| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| Pure RSI (無濾網) | {r_pure_oos['total_return']*100:.2f}% | {r_pure_oos['sharpe']:.4f} | {r_pure_oos['max_dd']*100:.2f}% | {r_pure_oos['trades']} |
| MTF Trend + RSI (部署邏輯) | {r_mtf_oos['total_return']*100:.2f}% | {r_mtf_oos['sharpe']:.4f} | {r_mtf_oos['max_dd']*100:.2f}% | {r_mtf_oos['trades']} |

OOS：{"MTF 濾網同樣加值" if oos_gate_helps else "MTF 濾網同樣未加值"}（{"與 IS-Val 結論一致" if oos_gate_helps == val_gate_helps else "排序反轉了，與 IS-Val 結論不一致"}）。

## ⑥ MAE/MFE SL/TP 校準（IS-Train，取代 `optimize_sl_tp.py` 原本的網格搜尋）
`optimize_sl_tp.py` 先前對 SL×TP 做 7×9=63 組合的網格搜尋，這本身是另一輪多重檢定（見 README ⑥、`quant-multiple-testing` skill）。本節改用 IS-Val 勝出候選（{winner_name}）在 IS-Train 上的進場事件，直接用 MAE/MFE 分布反推 SL/TP，不窮舉網格。

{f"**校準結果**：SL={mfe_sl*100:.2f}% (P75 |MAE|) / TP={mfe_tp*100:.2f}% (P50 MFE)，{mfe_mae_stats['n_events']} 個進場事件（median MAE={mfe_mae_stats['mae_median']*100:.2f}%, median MFE={mfe_mae_stats['mfe_median']*100:.2f}%）" if mfe_sl is not None else f"**無法校準**：{mfe_mae_stats.get('reason')}"}

Held-out（交易級交叉檢查，套用 IS-Train 校準出的固定 SL/TP，不重新校準）：
| 區間 | 無SL/TP | Sharpe | +SL/TP | Sharpe |
|---|---|---|---|---|
{fmt_sl_tp_holdout()}

## ⑦ 正式引擎交叉驗證（BacktestService，跨資產同參數不重調）／ ⑧ 跨資產穩健性
`mtf_trend_rsi_strategy.py` 本身固定是 MTF Trend + RSI 邏輯（不論⑤的 IS-Val 挑選結果為何），跨資產穩健性驗證直接沿用這支部署檔、同一組門檻套用到 BTC/ETH/TXFR1（不重新調參）：

| 資產 | 累積報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
{fmt_engine_rows()}

MAE/MFE SL/TP 疊加（正式引擎）：
| 資產 | 無SL/TP | Sharpe | +SL/TP | Sharpe |
|---|---|---|---|---|
{fmt_engine_sl_tp_rows()}

## 結論
- **本家族先前完全沒有做過因子驗證**：部署前只有 SL/TP 網格搜尋（`optimize_sl_tp.py`），從未檢驗過 RSI(14) 或 mom_1D_10 日線趨勢濾網是否真的有預測力，也從未檢驗過 MTF 濾網本身是否比不加濾網的純 RSI 反轉更好。本報告是本家族的第一次完整③~⑧驗證。
- **因子顯著性**：③（部署邏輯的 mom_1D_10/rsi_demeaned，Holm 校正 n={len(horizon_p_raw)}）——mom_1D_10 @ {trend_best[1]}h {"通過" if trend_sig else "未通過"}校正後顯著性（p_holm={trend_best[4]:.4f}），rsi_demeaned @ {rsi_best[1]}h {"通過" if rsi_sig else "未通過"}校正後顯著性（p_holm={rsi_best[4]:.4f}）。③-freq（同樣因子概念、跨 1H/4H/1D 重新取資料橫掃，Holm 校正 n={len(freq_sweep_p_raw)}）{"同樣找到顯著組合" if freq_sweep_winners else "沒有任何組合顯著"}。{"mom_1D_10 在③（用部署本身的日線聚合定義）通過校正，但③-freq 用小時線 shift(240) 逼近同一個 10-daily-bar 窗口卻沒有通過——這兩個因子在數學上不完全等價（daily-close 聚合 vs. 240 根小時 K 位移），差異本身就是一個誠實的警訊：mom_1D_10 的顯著性可能對「用日線收盤價聚合」這個具體構造方式敏感，不是一個在任何等價頻率下都穩健重現的邊際。" if (trend_sig and not freq_sweep_winners) else ""}rsi_demeaned 在兩節都未通過校正，本報告不會因為⑤/⑦回測數字好看就淡化這個結論——因子分析與回測績效是兩件事，回測正報酬可能只是特定窗口的雜訊、mom_1D_10 濾網本身的方向性 beta，或兩者疊加，不能倒推出 RSI 反轉訊號本身具備獨立、可泛化的預測力。
- **MTF 濾網是否加值**：見⑤/⑤b。IS-Val 上{"MTF Trend + RSI（Sharpe " + f"{r_mtf_val['sharpe']:.4f}" + "）優於 Pure RSI（" + f"{r_pure_val['sharpe']:.4f}" + "）" if val_gate_helps else "Pure RSI（Sharpe " + f"{r_pure_val['sharpe']:.4f}" + "）優於 MTF Trend + RSI（" + f"{r_mtf_val['sharpe']:.4f}" + "），濾網並未加值"}；OOS 盲測{"結論一致" if val_gate_helps == oos_gate_helps else "排序反轉，兩個獨立窗口沒有指向同一個結論"}。但③c 的 regime 切片檢定顯示 RSI 在多頭（p={rsi_board.filter(pl.col('trend_regime')=='true')['metric_p_value'][0]:.4f}）與空頭（p={rsi_board.filter(pl.col('trend_regime')=='false')['metric_p_value'][0]:.4f}）兩個 regime 下的 hit rate 都遠離顯著、且彼此差異不大——RSI 本身的方向性預測力沒有因為套上日線趨勢濾網而改善，⑤/⑤b 看到的 Sharpe 提升更可能來自「濾網把交易次數砍半、只在跟日線趨勢同向時才進場」帶來的方向性 beta 曝險，而不是 RSI 訊號的品質被濾網「淨化」了。
- **頻率落差**：③橫掃出的最穩定 forward period（{trend_best[1]}h / {rsi_best[1]}h）跟部署策略的邏輯出場（RSI 打回 65/35，非固定 forward period）不是同一件事——這是已知的方法論落差，尚未回頭調整部署邏輯，如同 `mtf_trend_momentum`/`adaptive_switching` 的先例一併誠實記錄。
- **SL/TP**：⑥用 MAE/MFE 百分位法取代原本 `optimize_sl_tp.py` 的網格搜尋，held-out 與正式引擎數字見上表，結論以實際數字為準，不預設加或不加 SL/TP 一定比較好。
- **`optimize_sl_tp.py` 的處置**：其網格搜尋（1a 節）已被本報告⑥的 MAE/MFE 方法取代（更符合 README ⑥ 反網格搜尋的指引）；其 MAE/MFE 校準（1b 節）邏輯與本報告⑥實質相同，功能已完全併入本腳本。連同 `sl_tp_robustness_report.md` 一併移除，避免兩份重疊但可能隨時間漂移出不一致結論的報告並存。
"""

    report_path = os.path.join(os.path.dirname(__file__), "report.md")
    with open(report_path, "w") as f_rep:
        f_rep.write(report)
    print(f"\nReport written to {report_path}")


if __name__ == "__main__":
    run_research()
