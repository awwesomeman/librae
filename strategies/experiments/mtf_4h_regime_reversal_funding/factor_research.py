import os
import sys
import json
import numpy as np
import pandas as pd

PROJECT_ROOT = "/Users/jason/Desktop/quantdinger"
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from strategies.utils.cached_kline import get_crypto_kbars_df
from strategies.utils.universe import TICKERS
from strategies.utils.funding import attach_funding_features
from strategies.utils.engine_check import run_engine_cross_check

STRATEGY_DIR = os.path.dirname(__file__)
STRATEGY_FILE = os.path.join(STRATEGY_DIR, "mtf_4h_regime_reversal_funding_strategy.py")

# Data Splits
IS_TRAIN_START = "2024-08-01"
IS_TRAIN_END   = "2025-04-30"
IS_VAL_END     = "2025-09-30"
FULL_END       = "2026-06-01"

def run_mae_mfe_calibration():
    print("\n=======================================================")
    print("=== Stage ⑥: MAE / MFE Trade Distribution & Risk Calibration ===")
    print("=======================================================")
    
    cfg = TICKERS["BTC"]
    df = get_crypto_kbars_df(cfg["exchange_id"], cfg["symbol"], "1h", IS_TRAIN_START, IS_VAL_END)
    df = df.reset_index().rename(columns={"Datetime": "datetime"})
    df = df.sort_values("datetime").reset_index(drop=True)
    
    # Run standalone indicator simulation on IS-Val period
    df["date"] = df["datetime"]
    df_val = df[df["datetime"] >= IS_TRAIN_END].copy().reset_index(drop=True)
    
    # We can run the exact logic from mtf_4h_regime_reversal_funding_strategy.py
    # Compute daily mom_1D_10
    df_val["date_only"] = df_val["datetime"].dt.date
    daily_df = df_val.groupby("date_only").agg({"Close": "last"}).rename(columns={"Close": "d_close"})
    daily_df["mom_1D_10"] = daily_df["d_close"] / daily_df["d_close"].shift(10) - 1.0
    df_val = df_val.merge(daily_df[["mom_1D_10"]], left_on="date_only", right_index=True, how="left")
    
    # Compute 4H features
    df_4h = df_val.set_index("datetime").resample("4h").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"
    }).dropna(subset=["Close"]).copy()
    
    df_4h["roc_3"] = df_4h["Close"] / df_4h["Close"].shift(3) - 1.0
    delta = df_4h["Close"].diff()
    gain = (delta.clip(lower=0)).rolling(6).mean()
    loss = (-delta.clip(upper=0)).rolling(6).mean()
    rs = gain / (loss + 1e-9)
    df_4h["rsi_6"] = 100.0 - (100.0 / (1.0 + rs)) - 50.0
    df_4h = attach_funding_features(df_4h.reset_index().rename(columns={"datetime": "date"}), "BTC", IS_TRAIN_START, IS_VAL_END)
    
    if "funding_rate" in df_4h.columns:
        f_mean = df_4h["funding_rate"].rolling(6).mean()
        f_std = df_4h["funding_rate"].rolling(6).std()
        df_4h["funding_z_1d"] = ((df_4h["funding_rate"] - f_mean) / (f_std + 1e-9)).fillna(0.0)
    else:
        df_4h["funding_z_1d"] = 0.0
        
    df_4h_reset = df_4h.rename(columns={"date": "datetime"})[["datetime", "roc_3", "rsi_6", "funding_z_1d"]]
    df_val["datetime"] = pd.to_datetime(df_val["datetime"]).dt.tz_localize(None).astype("datetime64[ns]")
    df_4h_reset["datetime"] = pd.to_datetime(df_4h_reset["datetime"]).dt.tz_localize(None).astype("datetime64[ns]")
    df_val = pd.merge_asof(df_val, df_4h_reset.sort_values("datetime"), on="datetime", direction="backward")
    
    # State Machine
    regime_thresh = 0.03
    roc_buy = -0.01
    rsi_buy = -35.0
    roc_sell = 0.025
    rsi_sell = 35.0
    funding_z_long = 1.5
    funding_z_short = -2.0

    pos = 0.0
    bars_held = 0
    trades = []
    current_trade = None
    
    for i in range(len(df_val)):
        row = df_val.iloc[i]
        m_10, r_3, rsi_val, fz_val = row["mom_1D_10"], row["roc_3"], row["rsi_6"], row["funding_z_1d"]
        price = row["Close"]
        high_p = row["High"]
        low_p = row["Low"]
        dt = row["datetime"]
        
        if np.isnan(m_10) or np.isnan(r_3) or np.isnan(rsi_val):
            continue
            
        is_ranging = abs(m_10) < regime_thresh
        
        if pos == 0.0:
            if is_ranging:
                if r_3 < roc_buy and rsi_val < rsi_buy:
                    pos = 1.0
                    current_trade = {"entry_time": str(dt), "entry_price": price, "dir": 1.0, "max_high": high_p, "min_low": low_p, "regime": "ranging"}
                elif r_3 > roc_sell and rsi_val > rsi_sell:
                    pos = -1.0
                    current_trade = {"entry_time": str(dt), "entry_price": price, "dir": -1.0, "max_high": high_p, "min_low": low_p, "regime": "ranging"}
            else:
                if m_10 > 0 and fz_val > funding_z_long:
                    pos = 1.0
                    current_trade = {"entry_time": str(dt), "entry_price": price, "dir": 1.0, "max_high": high_p, "min_low": low_p, "regime": "trending"}
                elif m_10 < 0 and fz_val < funding_z_short:
                    pos = -1.0
                    current_trade = {"entry_time": str(dt), "entry_price": price, "dir": -1.0, "max_high": high_p, "min_low": low_p, "regime": "trending"}
            bars_held = 0
        else:
            bars_held += 1
            current_trade["max_high"] = max(current_trade["max_high"], high_p)
            current_trade["min_low"] = min(current_trade["min_low"], low_p)
            
            should_exit = False
            if is_ranging:
                if (-3.0 <= rsi_val <= 3.0) or (bars_held >= 12):
                    should_exit = True
            else:
                if (pos == 1.0 and fz_val < 0.8) or (pos == -1.0 and fz_val > -0.8) or (bars_held >= 16):
                    should_exit = True
                    
            if should_exit:
                exit_price = price
                ret = (exit_price / current_trade["entry_price"] - 1.0) * pos
                
                # Calculate MAE (Max Adverse Excursion percentage) & MFE (Max Favorable Excursion percentage)
                if pos == 1.0:
                    mae = (current_trade["min_low"] / current_trade["entry_price"] - 1.0) * 100.0  # negative %
                    mfe = (current_trade["max_high"] / current_trade["entry_price"] - 1.0) * 100.0 # positive %
                else:
                    mae = (1.0 - current_trade["max_high"] / current_trade["entry_price"]) * 100.0 # negative %
                    mfe = (1.0 - current_trade["min_low"] / current_trade["entry_price"]) * 100.0  # positive %
                    
                current_trade["exit_time"] = str(dt)
                current_trade["exit_price"] = exit_price
                current_trade["ret_pct"] = ret * 100.0
                current_trade["mae_pct"] = mae
                current_trade["mfe_pct"] = mfe
                current_trade["is_win"] = ret > 0
                trades.append(current_trade)
                pos = 0.0
                current_trade = None
                
    if not trades:
        print("No completed trades found in IS-Val for MAE/MFE calibration.")
        return 0.025, 0.06
        
    df_trades = pd.DataFrame(trades)
    wins = df_trades[df_trades["is_win"]]
    losses = df_trades[~df_trades["is_win"]]
    
    print(f"\n[IS-Val Trade Summary] Total Trades: {len(df_trades)} | Wins: {len(wins)} ({len(wins)/len(df_trades)*100:.1f}%) | Losses: {len(losses)}")
    print(f"Average Winning Return: +{wins['ret_pct'].mean():.2f}% | Average Losing Return: {losses['ret_pct'].mean():.2f}%")
    print(f"\n[MAE Distribution across all trades (Max Adverse Excursion)]")
    print(df_trades["mae_pct"].describe(percentiles=[0.05, 0.10, 0.25, 0.50, 0.75, 0.90]))
    
    print(f"\n[MFE Distribution across all trades (Max Favorable Excursion)]")
    print(df_trades["mfe_pct"].describe(percentiles=[0.10, 0.25, 0.50, 0.75, 0.90, 0.95]))
    
    # Derive natural SL cut point: where losses exceed winning MAE tolerance
    # Let's set SL at the 10th percentile of winning trades' MAE or 2.5% ~ 3.0%
    if not wins.empty:
        win_mae_p10 = abs(wins["mae_pct"].quantile(0.10))
        optimal_sl = max(round(win_mae_p10 / 100.0, 3), 0.020) # At least 2.0%
    else:
        optimal_sl = 0.025
        
    if not wins.empty:
        win_mfe_p75 = wins["mfe_pct"].quantile(0.75)
        optimal_tp = max(round(win_mfe_p75 / 100.0, 3), 0.050) # At least 5.0%
    else:
        optimal_tp = 0.060
        
    print(f"\n=> Calibrated Risk Boundaries: Optimal Stop-Loss (SL) = -{optimal_sl*100:.1f}% | Optimal Take-Profit (TP) = +{optimal_tp*100:.1f}%")
    return optimal_sl, optimal_tp

def main():
    print("=======================================================================")
    print("=== MTF 4H REGIME REVERSAL & FUNDING STRATEGY RESEARCH PIPELINE ===")
    print("=======================================================================")
    
    # 1. Run MAE/MFE calibration on IS-Val
    opt_sl, opt_tp = run_mae_mfe_calibration()
    
    # 2. Stage ⑦ Engine Cross-Check via BacktestService
    print("\n=======================================================================")
    print("=== Stage ⑦ & ⑧: Engine Cross-Check & Cross-Asset Validation ===")
    print("=======================================================================")
    
    variants = [
        ("No SL/TP Baseline", {}),
        (f"Calibrated SL=-{opt_sl*100:.1f}%/TP=+{opt_tp*100:.1f}%", {
            "risk": {"stopLossPct": opt_sl, "takeProfitPct": opt_tp}
        })
    ]
    
    print(f"\nExecuting run_engine_cross_check on [{STRATEGY_FILE}] across ['BTC', 'ETH']...")
    results = run_engine_cross_check(
        STRATEGY_FILE, ["BTC", "ETH"], IS_TRAIN_START, IS_VAL_END, variants=variants
    )
    
    print("\n[Engine Cross-Check Results Summary]")
    for (asset, label), res in results.items():
        if "error" in res:
            print(f"  - [{asset}] ({label}): ERROR => {res['error']}")
        else:
            s_res = res.get("summary", {})
            print(f"  - [{asset}] ({label}):")
            print(f"      Total Return: {s_res.get('total_return', 0.0):+.2f}% | Sharpe: {s_res.get('sharpe_ratio', 0.0):.4f} | MaxDD: {s_res.get('max_drawdown', 0.0):.2f}% | Trades: {s_res.get('total_trades', 0)}")
            
    print("\nAll Stage ⑥ ~ ⑧ checks complete! Ready to write final report.md.")

if __name__ == "__main__":
    main()
