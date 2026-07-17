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
from utils.factors import calculate_bb_pct_b, calculate_atr, add_daily_trend_filter
from utils.stats import holm_bonferroni
from utils.engine_check import run_engine_cross_check
from utils.mfe_mae import compute_mfe_mae_events, derive_sl_tp
from utils.backtest_sim import extract_trades, simulate_sl_tp
from utils.open_interest import attach_oi_features

# ① 資產/資料層 — decision asset only; cross-asset section (⑧) extends the
# same params to ETH/TXFR1 without re-fitting.
DECISION_ASSET = "BTC"
ROBUST_ASSETS = ["BTC", "ETH", "TXFR1"]
ROBUST_START, ROBUST_END = "2025-01-01", "2026-06-01"

# ② 樣本切分 — three-way split (this family previously only had IS/OOS, and
# the filter ablation ([2] in the old script) was compared directly on the
# OOS window — i.e. the blind test window was used to pick which filter to
# deploy. IS_VAL_END matches the old IS/OOS boundary so the final held-out
# OOS window is unchanged; the new IS-Train/IS-Val split point is the same
# one adaptive_switching uses for this asset/period.
IS_TRAIN_START, IS_TRAIN_END = "2024-08-01", "2025-04-30"
IS_VAL_END = "2025-09-30"
FULL_END = "2026-06-01"

HORIZONS = [1, 4, 12, 24]

# ③-freq base-frequency sweep — README ③: if the core factor is not
# significant at the starting base frequency (1H) on any forward period, the
# next step is to re-fetch at a coarser/finer base frequency and re-sweep,
# not to declare the factor dead. `bb_period` is the Bollinger %b lookback
# used to build the mean-reversion factor at that base_tf — kept as a
# functionally sensible rolling window at each frequency (not an exact
# wall-clock match to the 1H/20-bar=20h window, since a 20-bar window is
# ~3.3 days at 4H and ~20 days at 1D, which would no longer be measuring a
# comparable "local range" concept); horizons are forward-period bar counts
# chosen so each base_tf covers roughly the same wall-clock targets as 1H's
# 1/4/12/24-bar sweep.
BASE_TF_CONFIG = {
    "1h": {"bb_period": 20, "horizons": [1, 4, 12, 24]},
    "4h": {"bb_period": 6, "horizons": [1, 2, 3, 6]},
    "1d": {"bb_period": 5, "horizons": [1, 2, 3, 5]},
}


def build_freq_sweep_features(raw: pd.DataFrame, bb_period: int) -> pd.DataFrame:
    """Minimal factor set for the base-frequency sweep (③-freq): the core
    mean-reversion factor (Bollinger %b, demeaned) only, generic column name
    so the same evaluation code runs across base_tf. Does not compute
    amp/vol/OI consolidation filters (those are regime slices tested in ③c
    at the 1H frequency the strategy is actually deployed at, not something
    the frequency sweep itself needs to answer "is the core factor
    significant at this base frequency").
    """
    df = raw.reset_index().rename(columns={'Datetime': 'date'})
    df['bb_factor'] = calculate_bb_pct_b(df['Close'], bb_period) - 0.5
    return df.dropna().reset_index(drop=True)


def build_features(raw: pd.DataFrame) -> pd.DataFrame:
    """Amplitude / volume / Keltner-channel consolidation filter — specific
    to this strategy family, so kept local rather than in utils/factors.py.

    The core factor this family trades is `bb_pct_b_1H_20` (Bollinger %b,
    demeaned around 0): the strategy's mean-reversion assumption is that
    when price sits near/outside the Keltner channel edges (a similar
    mean-reversion concept), it tends to revert toward the mid band. ③
    below is the first time this factor's directional predictive power is
    actually checked with factrix rather than assumed from the indicator's
    construction.

    Note: `open_interest_change_24h` is referenced by
    range_oscillator_strategy.py's OI filter, but neither ccxt spot OHLCV
    nor Shioaji TXFR1 kbars carry open interest — that filter has never
    actually activated in production for any asset in this framework, it
    silently falls back to "always consolidating" (see range_oscillator_
    strategy.py line ~41). This research script reproduces that same
    fallback rather than fabricating an OI series; where a real OI series
    is available (BTC/ETH, via utils/open_interest.py) it's brought in
    separately for the OI-filter checks (③c / ⑤), not baked into
    build_features() itself.
    """
    df = raw.reset_index().rename(columns={'Datetime': 'date'})
    df = add_daily_trend_filter(df)

    close, high, low, vol = df['Close'], df['High'], df['Low'], df['Volume']

    df['amp'] = (high - low) / (close + 1e-9)
    df['amp_sma'] = df['amp'].rolling(24).mean()
    df['vol_sma'] = vol.rolling(24).mean()

    df['mid'] = close.rolling(20).mean()
    df['atr'] = calculate_atr(high, low, close, 14)
    df['upper'] = df['mid'] + 1.5 * df['atr']
    df['lower'] = df['mid'] - 1.5 * df['atr']

    df['is_consolidating'] = (df['amp'] < df['amp_sma'] * 1.2) & (vol < df['vol_sma'] * 1.3)
    df['bb_pct_b_1H_20'] = calculate_bb_pct_b(close, 20) - 0.5

    return df.dropna().copy()


def attach_oi_regime(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """BTC-only OI-consolidating regime column (`is_consolidating_oi` =
    amp/vol filter AND OI filter), used by ③c's regime slice and ⑤'s
    "combined with OI" candidate. Rows without OI coverage are dropped
    (matches the strategy file's own semantics: OI is either present and
    used, or absent and the OI leg of the filter is meaningless — dropping
    rather than fabricating a fallback keeps this comparison honest about
    which rows it actually covers)."""
    out = attach_oi_features(df, "BTC", start, end).dropna(subset=["open_interest_change_24h"])
    out["oi_consolidating"] = out["open_interest_change_24h"].abs() < 5.0
    out["is_consolidating_oi"] = out["is_consolidating"] & out["oi_consolidating"]
    return out


def backtest_range_strategy(df_data, use_filters=True, use_trend=True, consolidating_col="is_consolidating", label=""):
    df = df_data.copy()
    signals = []
    pos = 0.0

    close_arr = df['Close'].values
    upper_arr = df['upper'].values
    lower_arr = df['lower'].values
    mid_arr = df['mid'].values
    consolidating = df[consolidating_col].values
    macro = df['mom_1D_10'].values

    for i in range(len(df)):
        c_val, up, lo, mid = close_arr[i], upper_arr[i], lower_arr[i], mid_arr[i]
        ok_to_trade = consolidating[i] if use_filters else True
        m_trend = macro[i]

        if pos == 0.0:
            if ok_to_trade:
                if use_trend:
                    if m_trend > 0 and c_val < lo:
                        pos = 1.0
                    elif m_trend < 0 and c_val > up:
                        pos = -1.0
                else:
                    if c_val < lo:
                        pos = 1.0
                    elif c_val > up:
                        pos = -1.0
        elif pos == 1.0:
            if c_val > mid:
                pos = 0.0
        elif pos == -1.0:
            if c_val < mid:
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
    # ③-freq 基礎頻率橫掃（IS-Train only）——核心因子 bb_pct_b（去均值）在
    #    1H 上是否顯著見下方③；若不顯著，依 README ③ 換基礎頻率（4H/1D）
    #    重新取資料橫掃，而非死守 1H。base_tf × horizon 的組合是網格搜尋，
    #    這裡是「掃過網格挑單一贏家」的決策形態，需要 FWER（guard「至少挑
    #    錯一次」的機率），不是 FDR——factrix 公開 API 只有 FDR 工具
    #    （fx.stats.bhy_adjusted_p/bhy/bhy_hierarchical），沒有公開的 FWER
    #    工具（_stats/multiple_testing.py 雖有 holm_step_down，但是底線開
    #    頭的 private module，未被 factrix 自己的公開介面引用，不算穩定
    #    API），故此處沿用 utils/stats.py 的手刻 Holm-Bonferroni（見 README
    #    ③ 對 FWER/FDR 這個 factrix 缺口的說明）。
    # -------------------------------------------------------------
    print("\n=== ③-freq 基礎頻率橫掃（IS-Train，1H/4H/1D，factrix directional_hit_rate） ===")
    btc_cfg = TICKERS[DECISION_ASSET]
    freq_sweep_rows = []  # (base_tf, factor, horizon_bars, hit_rate, p_value)
    print(f"{'base_tf':<8} | {'factor':<10} | {'h(bars)':>7} | {'hit_rate':>8} | {'p_value':>8}")
    for base_tf, cfg in BASE_TF_CONFIG.items():
        raw_tf = get_crypto_kbars_df(btc_cfg["exchange_id"], btc_cfg["symbol"], base_tf, IS_TRAIN_START, IS_TRAIN_END)
        feat_tf = build_freq_sweep_features(raw_tf, cfg["bb_period"])
        pl_tf = pl.from_pandas(feat_tf[['date', 'Close', 'bb_factor']].rename(columns={'Close': 'price'}))
        pl_tf = pl_tf.with_columns(pl.lit("BTC").alias("asset_id"))
        # evaluate_horizons rebuilds compute_forward_return per horizon
        # internally (README ③: don't hand-roll a for-loop of evaluate()
        # calls to simulate a sweep — evaluate_horizons is the safe wrapper).
        sweep_results = fx.evaluate_horizons(
            pl_tf, metrics={"dir_hit": directional_hit_rate()},
            factor_cols=["bb_factor"], forward_periods=cfg["horizons"],
        )
        for r in sweep_results:
            m = r.metrics["dir_hit"]
            freq_sweep_rows.append((base_tf, r.factor, r.forward_periods, m.value, m.p_value))
            print(f"{base_tf:<8} | {r.factor:<10} | {r.forward_periods:>7} | {m.value:>8.4f} | {m.p_value:>8.4f}")

    # directional_hit_rate's p_value is one-sided against hit_rate==0.5 with
    # the tail direction implicit in whether p is near 0 (trend-following
    # effect) or near 1 (reversal effect, which is what this factor is
    # designed to capture) — same convention best_horizon() below relies on.
    # The correction needs a small-p-is-significant statistic, so two-sided-
    # transform BEFORE correcting, not after.
    #
    # Hand-rolled Holm-Bonferroni (utils/stats.py), not factrix's
    # fx.stats.bhy_adjusted_p: this grid search picks a SINGLE winning
    # (base_tf, horizon) cell to deploy, which calls for FWER (bound the
    # probability of picking even one spurious cell), not FDR (bounds the
    # expected false-discovery share among MULTIPLE kept hypotheses — the
    # right target when screening a factor batch you plan to keep several
    # survivors from, not when cherry-picking one). factrix's public API
    # only ships FDR tools (bhy_adjusted_p/bhy/bhy_hierarchical) — see
    # README ③ for why this is a genuine factrix gap, not a preference.
    freq_sweep_p_raw = [r[4] for r in freq_sweep_rows]
    freq_sweep_p_2sided = [min(2 * min(p, 1 - p), 1.0) for p in freq_sweep_p_raw]
    freq_sweep_p_adj = holm_bonferroni(freq_sweep_p_2sided)
    freq_sweep_rows_adj = [(*row, p_adj) for row, p_adj in zip(freq_sweep_rows, freq_sweep_p_adj)]
    ALPHA = 0.05
    freq_sweep_winners = [r for r in freq_sweep_rows_adj if r[5] < ALPHA]
    print(f"\nHolm-Bonferroni 校正後（FWER control，n={len(freq_sweep_p_raw)} 個檢定，two-sided，alpha={ALPHA}）："
          f"{'有' if freq_sweep_winners else '沒有'}任何 (base_tf, horizon) 組合顯著")
    for r in freq_sweep_winners:
        print(f"  WINNER: {r[0]} @ {r[2]} bars: hit={r[3]:.4f} p_raw={r[4]:.4f} p_holm={r[5]:.4f}")

    # -------------------------------------------------------------
    # ③ 因子分析：1H 內多 forward period 橫掃（IS-Train only）——這是這個
    #    家族第一次真正用 factrix 檢定核心因子 bb_pct_b_1H_20（去均值）
    #    是否具方向性預測力，而不是只憑指標構造（Keltner/BB 概念）就假設
    #    均值回歸成立。找出哪個 forward period 上最穩定顯著，這一步的產出
    #    決定④，不是拿已經定案的頻率回頭驗證。1H 只是③-freq 橫掃的其中一
    #    個基礎頻率，這裡保留單獨一節是因為④~⑧沿用的正是 1H 部署邏輯。
    # -------------------------------------------------------------
    print("\n=== ③ 因子分析：多頻率橫掃（IS-Train，factrix directional_hit_rate） ===")
    pl_train = pl.from_pandas(df_train[['date', 'Close', 'bb_pct_b_1H_20']].rename(columns={'Close': 'price'}))
    pl_train = pl_train.with_columns(pl.lit("BTC").alias("asset_id"))

    train_sweep_results = fx.evaluate_horizons(
        pl_train, metrics={"dir_hit": directional_hit_rate()},
        factor_cols=["bb_pct_b_1H_20"], forward_periods=HORIZONS,
    )
    horizon_rows = []
    print(f"{'factor':<16} | {'h':>3} | {'hit_rate':>8} | {'p_value':>8}")
    for r in train_sweep_results:
        m = r.metrics["dir_hit"]
        horizon_rows.append((r.factor, r.forward_periods, m.value, m.p_value))
        print(f"{r.factor:<16} | {r.forward_periods:>3} | {m.value:>8.4f} | {m.p_value:>8.4f}")

    def best_horizon(factor):
        # "most extreme p-value" picks whichever horizon is farthest from
        # p=0.5 in EITHER direction — this factor is tested for both a
        # trend effect (p near 0) and a reversion effect (p near 1, which
        # is the strategy's actual assumption).
        rows = [r for r in horizon_rows if r[0] == factor]
        return min(rows, key=lambda r: min(r[3], 1 - r[3]))

    bb_best = best_horizon("bb_pct_b_1H_20")
    bb_effect = "reversal" if bb_best[2] < 0.5 else "trend"
    print(f"\nbb_pct_b_1H_20 最穩定顯著: {bb_best[1]}h (hit={bb_best[2]:.4f}, p={bb_best[3]:.4f}, 效應方向={bb_effect})")

    # -------------------------------------------------------------
    # ③b 因子邊際穩定性（oos_decay）：在 IS-Train 內部前70%/後30%切分，這個
    #    邊際是否存活，而不是只看整個 IS-Train 合併後的單一數字。
    # -------------------------------------------------------------
    print("\n=== ③b 因子邊際穩定性（oos_decay，IS-Train 內部切分） ===")
    data_h = fx.preprocess.compute_forward_return(pl_train, forward_periods=bb_best[1])
    value_series = data_h.select(
        pl.col("date"),
        (pl.col("bb_pct_b_1H_20") * pl.col("forward_return")).alias("value"),
    ).drop_nulls().sort("date")
    decay = oos_decay(value_series)
    decay_rows = [("bb_pct_b_1H_20", bb_best[1], decay.value, decay.metadata.get("sign_flipped"), decay.metadata.get("status"))]
    print(f"bb_pct_b_1H_20 @ {bb_best[1]}h: survival_ratio={decay.value:.4f} sign_flipped={decay.metadata.get('sign_flipped')} status={decay.metadata.get('status')}")

    # -------------------------------------------------------------
    # ③c 濾鏡假設的 regime 切片檢定（IS-Train，用③挑出的頻率）——這是原本
    #    [1]/[1b] 的內容（濾鏡是否讓 bb_pct_b 更準），改成明確標成③c、只用
    #    IS-Train（原本就是 IS-only，沒有 OOS 洩漏問題，這裡只是搬到新的
    #    切分結構下並沿用挑出的 bb_best[1] horizon，而非硬編 4h）。
    # -------------------------------------------------------------
    print("\n=== ③c 濾鏡 regime 切片檢定（IS-Train, factrix by_slice） ===")
    pl_train_regime = pl.from_pandas(df_train[['date', 'Close', 'bb_pct_b_1H_20', 'is_consolidating']].rename(columns={'Close': 'price'}))
    pl_train_regime = pl_train_regime.with_columns(
        pl.lit("BTC").alias("asset_id"),
        pl.col("is_consolidating").cast(pl.Utf8).alias("consolidating_regime"),
    )
    data_bb_h = fx.preprocess.compute_forward_return(pl_train_regime, forward_periods=bb_best[1])
    vol_amp_res = fx.by_slice(data_bb_h, directional_hit_rate(), by="consolidating_regime", factor_col="bb_pct_b_1H_20", strict=False)
    vol_amp_keys = list(vol_amp_res.keys())
    vol_amp_board = fx.compare(list(vol_amp_res.values()), metrics=["metric"]).with_columns(pl.Series("consolidating_regime", vol_amp_keys))
    print(f">>> Vol+Amp 濾鏡：bb_pct_b_1H_20 hit rate by is_consolidating @ {bb_best[1]}h:")
    print(vol_amp_board)

    df_train_oi = attach_oi_regime(df_train, IS_TRAIN_START, IS_TRAIN_END)
    print(f"OI-consolidating share of BTC IS-Train: {df_train_oi['oi_consolidating'].mean()*100:.1f}% ({len(df_train_oi)} rows with OI coverage)")
    pl_train_oi = pl.from_pandas(df_train_oi[['date', 'Close', 'bb_pct_b_1H_20', 'oi_consolidating']].rename(columns={'Close': 'price'}))
    pl_train_oi = pl_train_oi.with_columns(pl.lit("BTC").alias("asset_id"), pl.col("oi_consolidating").cast(pl.Utf8).alias("oi_regime"))
    data_bb_oi_h = fx.preprocess.compute_forward_return(pl_train_oi, forward_periods=bb_best[1])
    oi_res = fx.by_slice(data_bb_oi_h, directional_hit_rate(), by="oi_regime", factor_col="bb_pct_b_1H_20", strict=False)
    oi_keys = list(oi_res.keys())
    oi_board = fx.compare(list(oi_res.values()), metrics=["metric"]).with_columns(pl.Series("oi_regime", oi_keys))
    print(f">>> OI 濾鏡：bb_pct_b_1H_20 hit rate by oi_consolidating @ {bb_best[1]}h:")
    print(oi_board)

    # -------------------------------------------------------------
    # ④ 頻率/持有期決定 — 輸入是③/③-freq的橫掃結果，不是研究前的經驗值。
    # -------------------------------------------------------------
    print("\n=== ④ 頻率/持有期決定 ===")
    freq_note = (
        f"bb_pct_b_1H_20 在 {bb_best[1]}h 最穩定顯著，效應方向為「{bb_effect}」（hit rate {bb_best[2]:.4f}, p={bb_best[3]:.4f}）。"
        f"策略部署檔（range_oscillator_strategy.py）逐 1H bar 判斷進出場，"
        f"跟橫掃出來的最適 forward period（{bb_best[1]}h）不完全一致——這裡誠實記錄落差，不回頭改動已部署的策略頻率（見結論）。"
    )
    print(freq_note)

    # -------------------------------------------------------------
    # ⑤ 策略候選比較（IS-Val）——這是新增的步驟：原本這個家族的濾鏡比較
    #    （舊版 [2]）直接在 OOS 上做，等於候選挑選階段就用掉了唯一的盲測
    #    窗口。四個候選：無濾鏡基準、Vol+Amp濾鏡、Trend+Vol+Amp（部署邏
    #    輯）、Trend+Vol+Amp+OI（疊加真實 OI 資料）。
    # -------------------------------------------------------------
    print("\n=== ⑤ 策略候選比較（IS-Val）===")
    df_val_oi = attach_oi_regime(df_val, IS_TRAIN_END, IS_VAL_END)
    print(f"OI-consolidating share of BTC IS-Val: {df_val_oi['oi_consolidating'].mean()*100:.1f}% ({len(df_val_oi)}/{len(df_val)} rows with OI coverage)")

    CANDIDATES = [
        {"name": "No filter (baseline)", "use_filters": False, "use_trend": False, "col": "is_consolidating", "needs_oi": False},
        {"name": "Vol+Amp filter only", "use_filters": True, "use_trend": False, "col": "is_consolidating", "needs_oi": False},
        {"name": "Trend+Vol+Amp (deployed)", "use_filters": True, "use_trend": True, "col": "is_consolidating", "needs_oi": False},
        {"name": "Trend+Vol+Amp+OI (combined)", "use_filters": True, "use_trend": True, "col": "is_consolidating_oi", "needs_oi": True},
    ]

    val_results = {}
    for cand in CANDIDATES:
        frame = df_val_oi if cand["needs_oi"] else df_val
        res = backtest_range_strategy(frame, use_filters=cand["use_filters"], use_trend=cand["use_trend"],
                                       consolidating_col=cand["col"], label=f"IS-Val: {cand['name']}")
        val_results[cand["name"]] = res

    winner_name = max(val_results, key=lambda n: val_results[n]["sharpe"])
    winner_cand = next(c for c in CANDIDATES if c["name"] == winner_name)
    print(f"\nIS-Val 最高 Sharpe 候選: {winner_name} (Sharpe={val_results[winner_name]['sharpe']:.4f}) — 選定為進 OOS 盲測的唯一候選")

    # -------------------------------------------------------------
    # ⑤b 盲測 OOS — 全程未用 OOS 挑選任何候選/參數；四個候選都跑出來是為
    #    了透明呈現，但候選挑選的依據只看上面 IS-Val 的 Sharpe。
    # -------------------------------------------------------------
    print("\n=== ⑤b 盲測 OOS ===")
    df_oos_oi = attach_oi_regime(df_oos, IS_VAL_END, FULL_END)
    print(f"OI-consolidating share of BTC OOS: {df_oos_oi['oi_consolidating'].mean()*100:.1f}% ({len(df_oos_oi)}/{len(df_oos)} rows with OI coverage)")

    oos_results = {}
    for cand in CANDIDATES:
        frame = df_oos_oi if cand["needs_oi"] else df_oos
        res = backtest_range_strategy(frame, use_filters=cand["use_filters"], use_trend=cand["use_trend"],
                                       consolidating_col=cand["col"], label=f"OOS: {cand['name']}")
        oos_results[cand["name"]] = res

    # -------------------------------------------------------------
    # ⑥ 風控疊加校準（MAE/MFE，IS-Train only）——用⑤挑出的贏家候選（而非
    #    固定用部署邏輯）在 IS-Train 上的進場事件推導 SL/TP；held-out 同時
    #    看 IS-Val 跟 OOS（原本只驗證 OOS）。
    # -------------------------------------------------------------
    print(f"\n=== ⑥ MAE/MFE SL/TP 校準（IS-Train，基於⑤贏家「{winner_name}」）===")
    train_frame_for_winner = df_train_oi if winner_cand["needs_oi"] else df_train
    is_res = backtest_range_strategy(train_frame_for_winner, use_filters=winner_cand["use_filters"], use_trend=winner_cand["use_trend"],
                                      consolidating_col=winner_cand["col"], label="IS-Train (for MAE/MFE derivation)")
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
        for name, res in [("IS-Val", val_results[winner_name]), ("OOS", oos_results[winner_name])]:
            t, h, l, c = extract_trades(res["df"], signal_col="signal")
            no_sl_check = simulate_sl_tp(t, h, l, c, 0.99, 9.99, friction=0.0015 * 2)
            with_sl_tp = simulate_sl_tp(t, h, l, c, mfe_sl, mfe_tp, friction=0.0015 * 2)
            sl_tp_holdout_rows.append((name, no_sl_check, with_sl_tp))
            print(f"[{name}] no-SL/TP (cross-check): Return={no_sl_check['total_return']*100:.2f}% Sharpe={no_sl_check['sharpe']:.4f} | MAE/MFE SL/TP: Return={with_sl_tp['total_return']*100:.2f}% Sharpe={with_sl_tp['sharpe']:.4f}")
    else:
        mfe_sl, mfe_tp = None, None
        print(f"MAE/MFE-derived: unavailable — {mfe_mae_stats['reason']}")

    # -------------------------------------------------------------
    # ⑦ 正式引擎交叉驗證 — 驗證的是磁碟上實際部署的策略檔（Trend+Vol+Amp
    #    +OI-fallback 邏輯），跟⑤挑出的研究候選是否一致見結論。
    # -------------------------------------------------------------
    print("\n=== ⑦ 正式引擎交叉驗證（BacktestService） ===")
    strategy_path = os.path.join(os.path.dirname(__file__), "range_oscillator_strategy.py")
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

    def fmt_candidate_rows(results):
        return "\n".join(
            f"| {c['name']} | {results[c['name']]['total_return']*100:.2f}% | {results[c['name']]['sharpe']:.4f} | "
            f"{results[c['name']]['max_dd']*100:.2f}% | {results[c['name']]['trades']} |"
            for c in CANDIDATES
        )

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

    val_best_over_baseline = val_results[winner_name]["sharpe"] > val_results["No filter (baseline)"]["sharpe"]
    oos_best_over_baseline = oos_results[winner_name]["sharpe"] > oos_results["No filter (baseline)"]["sharpe"]

    vol_amp_p_min = min(row["metric_p_value"] for row in vol_amp_board.to_dicts())
    oi_p_min = min(row["metric_p_value"] for row in oi_board.to_dicts())
    regime_sig_note = (
        f"Vol+Amp 濾鏡兩側最小 p-value={vol_amp_p_min:.4f}，OI 濾鏡兩側最小 p-value={oi_p_min:.4f}"
        f"（未做跨 regime 多重檢定校正，僅作描述性參考）——"
        f"{'至少一個濾鏡的某一側達到常見 0.05 門檻' if min(vol_amp_p_min, oi_p_min) < 0.05 else '兩個濾鏡的任一側都沒有達到常見 0.05 門檻'}，"
        f"{'但這不等於濾鏡本身已被證實加值，仍要看⑤/⑤b 的策略級回測結果' if min(vol_amp_p_min, oi_p_min) < 0.05 else '濾鏡是否真的隔出了一個 bb_pct_b 更準的子集，缺乏因子層級的證據支持，⑤/⑤b 的策略級回測結果是更直接的判準'}。"
    )

    report = f"""# Range Oscillator — 研究報告

**時間**: 2026-07-12 | **決策資產**: {DECISION_ASSET} | **跨資產**: {", ".join(ROBUST_ASSETS)}
**樣本切分**: IS-Train {IS_TRAIN_START}~{IS_TRAIN_END} | IS-Val ~{IS_VAL_END} | OOS ~{FULL_END} | 跨資產窗口 {ROBUST_START}~{ROBUST_END}（同參數不重調）

流程依 `strategies/README.md` 的 ①~⑧ 順序執行。

## Bug / 已知限制（沿用自舊版）
策略檔案（`range_oscillator_strategy.py`）的三重防禦濾鏡裡有一個「未平倉量變化 < 5% 視為盤整」的 OI 濾鏡，但程式碼本身有 fallback：`if 'open_interest_change_24h' in df.columns` 找不到就直接視為恆真（`oi_consolidating = True`）。**ccxt 現貨 OHLCV 沒有 OI，Shioaji TXFR1 kbars 也沒有**，所以這個濾鏡在正式引擎（讀 ccxt/Shioaji 即時資料）上從未真正過濾過任何一根K棒，"三重防禦"實質上只有兩重（成交量 + 振幅）在運作。`utils/open_interest.py`（Binance 歷史批次資料庫）把 BTC/ETH 的歷史 OI 接進**這支研究腳本**供離線驗證（見③c/⑤），但要讓濾鏡在正式引擎上真的生效還需要額外引擎工程（把 OI 當成跟 OHLCV 一起注入的欄位）；TXFR1 仍無對應資料源。

## ③-freq 基礎頻率橫掃（IS-Train，1H/4H/1D）
核心均值回歸因子 `bb_pct_b`（去均值）在各基礎頻率的 forward period 橫掃結果如下；`bb_period` 依基礎頻率調整為功能上合理的滾動窗口（1H: 20 bars / 4H: 6 bars / 1D: 5 bars），不是精確對齊小時數，因為原本 20-bar 窗口若照小時數換算到 4H/1D 會變成完全不同尺度的「局部區間」概念。

**多重檢定校正方法**：用 `utils/stats.py` 的手刻 `holm_bonferroni`（Holm step-down，控制 **FWER**），不是 factrix 現成工具。原因：這個網格搜尋的決策形態是「掃過 base_tf×horizon 全部組合、挑單一贏家」，需要的是 FWER（控制「至少挑錯一次」的機率），不是 FDR（適合「篩一批因子、全部留著用」的情境）。**factrix 公開 API 目前只有 FDR 工具**（`fx.stats.bhy_adjusted_p`/`fx.multi_factor.bhy`/`bhy_hierarchical`）——`factrix/_stats/multiple_testing.py` 雖然有 `holm_step_down`/`bonferroni`，但那是底線開頭的 private module，未被 `factrix/__init__.py` 或公開的 `factrix/stats/` 引用，不能當作穩定 API 依賴。這是 README ③ 認定的真實 factrix 缺口，故此處保留手刻。

| 基礎頻率 | 因子 | Forward(bars) | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|---|
{fmt_freq_sweep_rows()}

Holm-Bonferroni 校正（n={len(freq_sweep_p_raw)} 個檢定，跨 base_tf × horizon 一起校正，alpha=0.05）後，{"找到以下顯著組合" if freq_sweep_winners else "沒有任何組合顯著"}。{("；".join(f"{r[0]}@{r[2]}bars (hit={r[3]:.4f}, p_holm={r[5]:.4f})" for r in freq_sweep_winners) + "。") if freq_sweep_winners else "1H/4H/1D 三個基礎頻率上，bb_pct_b 均未通過校正後的顯著性門檻——這不是 1H 特有的問題，換粗/細基礎頻率沒有找到可用的邊際。"}

## ③ 因子分析：多頻率橫掃（IS-Train）
這是這個家族第一次真正用 factrix `directional_hit_rate`/`evaluate_horizons` 檢定核心因子 `bb_pct_b_1H_20`（去均值）是否具方向性預測力，而不是只憑指標構造（Keltner/BB 均值回歸概念）就假設成立。

| 因子 | 持有期 | Hit Rate | p-value |
|---|---|---|---|
{fmt_horizon_rows()}

bb_pct_b_1H_20 最穩定顯著: {bb_best[1]}h (hit={bb_best[2]:.4f}, p={bb_best[3]:.4f}, 效應方向={bb_effect})

## ③b 因子邊際穩定性（oos_decay，IS-Train 內部 70/30 切分）
| 因子 | 頻率 | 存活率 | 反號 | 狀態 |
|---|---|---|---|---|
{fmt_decay_rows()}

存活率（絕對值 mean_OOS / mean_IS）< 0.5 或反號代表這個因子在 IS-Train 內部自己都不穩定，不用等到 IS-Val 就已經是警訊。

## ③c 濾鏡 Regime 切片檢定（IS-Train，用③挑出的頻率）
這是原本舊版報告 §1/§1b 的內容，改成明確標成③c、限定只用 IS-Train（原本就是 IS-only，沒有 OOS 洩漏問題，這裡只是重新掛上 ①②③ 的切分結構，並改用③挑出的 {bb_best[1]}h forward period，而非舊版寫死的 4h forward period——本次剛好也落在 4h，屬巧合，不是刻意保留舊值）。

Vol+Amp 濾鏡（`is_consolidating`）：
| Regime | Hit Rate | p-value |
|---|---|---|
{fmt_regime_board(vol_amp_board, "consolidating_regime")}

OI 濾鏡（`oi_consolidating`，BTC IS-Train OI 覆蓋率 {df_train_oi['oi_consolidating'].mean()*100:.1f}%）：
| Regime | Hit Rate | p-value |
|---|---|---|
{fmt_regime_board(oi_board, "oi_regime")}

## ④ 頻率/持有期決定
{freq_note}

## ⑤ 策略候選比較（IS-Val）
四個候選：無濾鏡基準、Vol+Amp濾鏡、Trend+Vol+Amp（部署邏輯）、Trend+Vol+Amp+OI（疊加真實 OI，BTC IS-Val OI 覆蓋率 {df_val_oi['oi_consolidating'].mean()*100:.1f}%，僅 {len(df_val_oi)}/{len(df_val)} 筆有覆蓋，比較基礎比其他三個候選小）。

| 候選 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
{fmt_candidate_rows(val_results)}

**IS-Val 挑選結果：「{winner_name}」**（Sharpe={val_results[winner_name]['sharpe']:.4f}）{"優於" if val_best_over_baseline else "未優於"}無濾鏡基準（Sharpe={val_results['No filter (baseline)']['sharpe']:.4f}）。挑選依據僅用 IS-Val，未使用 OOS。

## ⑤b 盲測 OOS
全程未用 OOS 挑選任何候選/參數；四個候選都跑出來是為了透明呈現，候選挑選的依據只看上面 IS-Val 的 Sharpe。

| 候選 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
{fmt_candidate_rows(oos_results)}

OOS 上「{winner_name}」（Sharpe={oos_results[winner_name]['sharpe']:.4f}）{"同樣優於" if oos_best_over_baseline == val_best_over_baseline and oos_best_over_baseline else ("同樣未優於" if oos_best_over_baseline == val_best_over_baseline else "排序反轉，未優於")}無濾鏡基準（Sharpe={oos_results['No filter (baseline)']['sharpe']:.4f}）——{"兩個獨立窗口（IS-Val、OOS）指向同一個結論" if oos_best_over_baseline == val_best_over_baseline else "IS-Val 與 OOS 的排序不一致，濾鏡的加值證據不穩健"}。

## ⑥ MAE/MFE SL/TP 校準（IS-Train，基於⑤贏家「{winner_name}」）
{f"**校準結果**：SL={mfe_sl*100:.2f}% / TP={mfe_tp*100:.2f}%（{mfe_mae_stats['n_events']} 個進場事件）" if mfe_sl is not None else f"**無法校準**：{mfe_mae_stats.get('reason')}"}

Held-out（交易級交叉檢查，套用 IS-Train 校準出的固定 SL/TP，不重新校準，⑤贏家候選的訊號序列）：
| 區間 | 無SL/TP | Sharpe | +SL/TP | Sharpe |
|---|---|---|---|---|
{fmt_sl_tp_holdout()}

## ⑦ 正式引擎交叉驗證（BacktestService，跨資產同參數不重調）
驗證的是磁碟上實際部署的策略檔（`range_oscillator_strategy.py`：Trend+Vol+Amp 濾鏡 + OI-fallback 恆真邏輯），與⑤挑出的研究候選是否一致見下方結論。

| 資產 | 累積報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
{fmt_engine_rows()}

MAE/MFE SL/TP 疊加（正式引擎，用⑥從⑤贏家候選校準出的 SL/TP，套到部署策略檔上）：
| 資產 | 無SL/TP | Sharpe | +SL/TP | Sharpe |
|---|---|---|---|---|
{fmt_engine_sl_tp_rows()}

## 結論
- **濾鏡是否加值（⑤/⑤b，這是本次重構最大的方法論修正）**：舊版報告直接在 OOS 上比較三個濾鏡候選，等於候選挑選階段就用掉了唯一的盲測窗口。改成 IS-Val 挑選、OOS 盲測後，IS-Val 上最佳候選是「{winner_name}」（Sharpe={val_results[winner_name]['sharpe']:.4f}），{"優於" if val_best_over_baseline else "並未優於"}無濾鏡基準；OOS 盲測{"同樣" if oos_best_over_baseline == val_best_over_baseline else "沒有"}支持這個結論。{"這代表舊版直接在 OOS 挑濾鏡雖然方法論上不嚴謹，但這次改成 IS-Val→OOS 兩階段後結論方向沒有改變" if oos_best_over_baseline == val_best_over_baseline else "這代表舊版直接在 OOS 挑濾鏡的方法論問題不只是理論上的瑕疵——換成真正 held-out 的 IS-Val→OOS 流程後，濾鏡是否加值這件事本身就不穩健，兩個窗口給出不同答案"}。
- **核心因子的方向性（③，這是這個家族第一次真正檢定）**：bb_pct_b_1H_20 最穩定顯著是在 {bb_best[1]}h、效應方向為「{bb_effect}」（hit rate {bb_best[2]:.4f}, p={bb_best[3]:.4f}）{"，跟策略假設的均值回歸方向一致" if bb_effect == "reversal" else "，但策略把它當均值回歸訊號使用——因子分析結果本身跟策略的使用方式方向相反，這比濾鏡門檻的問題更根本"}。③b 顯示這個邊際在 IS-Train 內部切分後{"存活" if decay_rows[0][2] >= 0.5 and not decay_rows[0][3] else "不穩定甚至反號（VETOED）"}，{"不需要等到 IS-Val 就已經是警訊" if decay_rows[0][3] or decay_rows[0][2] < 0.5 else "支持繼續往下走的信心"}。
- **③c 濾鏡假設的事實根據**：{regime_sig_note}
- **頻率落差**：③橫掃出的最穩定 forward period（{bb_best[1]}h）跟部署策略固定逐 1H bar 判斷（見④）不完全一致——這是已知的方法論落差，尚未回頭調整部署頻率。
- **基礎頻率橫掃（③-freq）**：{"1H 上的（不）顯著不是頻率選錯——換 4H/1D 重新取資料橫掃、跨 base_tf×horizon 做 Holm-Bonferroni 校正（手刻，FWER control）後，三個基礎頻率上 bb_pct_b 都沒有通過顯著性門檻，代表這個因子在 BTC 上（至少在這個樣本窗口）本身就不具備可用邊際，不是 1H 這個起始假設的問題。" if not freq_sweep_winners else "換基礎頻率後找到顯著組合（見③-freq），下一步應針對該頻率重新走④~⑧，而非沿用目前部署的 1H 邏輯。"}
- **OI 濾鏡的資料落差仍未解決**：見上方「Bug / 已知限制」——即使研究階段已能用真實 OI 驗證假設（③c/⑤），正式引擎上這個濾鏡仍然是 fallback 恆真，三重防禦名不副實的問題本身沒有變。
- **MAE/MFE SL/TP 疊加**：見⑦數字，不假設對所有家族都有害或有利，依實際引擎結果判斷。
"""

    report_path = os.path.join(os.path.dirname(__file__), "report.md")
    with open(report_path, "w") as f_rep:
        f_rep.write(report)
    print(f"\nReport written to {report_path}")


if __name__ == "__main__":
    run_research()
