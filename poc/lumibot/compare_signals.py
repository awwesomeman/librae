#!/usr/bin/env python3
"""Compare signals from reference strategy vs Lumibot implementation.

Usage:
    python -m poc.lumibot.compare_signals
"""
from __future__ import annotations

import sys
import uuid

import pandas as pd

from scripts.etl.core_features import (
    add_daily_trend_gate,
    add_trendpullback_features,
    resample_ohlcv,
)
from poc.lumibot.sample_data import generate_btc_1m
from poc.lumibot.signal_schema import signals_to_df
from poc.lumibot.reference_signals import extract_signals as extract_reference
from poc.lumibot.strategy_lumibot import TrendPullbackLumibot


def run_lumibot_signal_extraction(
    m1: pd.DataFrame,
    h1: pd.DataFrame,
    d1: pd.DataFrame,
    start: str,
    end: str,
    *,
    pull: float = 0.3,
    bn: int = 5,
    en: int = 20,
    run_id: str,
) -> pd.DataFrame:
    """Run the Lumibot strategy's signal detection in direct-call mode."""
    strategy = TrendPullbackLumibot.__new__(TrendPullbackLumibot)
    strategy.parameters = {
        "pull": pull, "bn": bn, "en": en, "run_id": run_id,
        "_h1": h1, "_d1": d1, "_m1": m1,
    }
    strategy.initialize()

    h = h1[(h1.index >= start) & (h1.index <= end)]
    for t in h.index:
        strategy.get_datetime = lambda _t=t: _t
        strategy.on_trading_iteration()

    return signals_to_df(strategy.signals)


def compare_signals(ref_df: pd.DataFrame, lumi_df: pd.DataFrame) -> dict:
    """Compute concordance metrics between reference and Lumibot signals."""
    if ref_df.empty and lumi_df.empty:
        return {
            "ref_count": 0,
            "lumi_count": 0,
            "exact_match": 0,
            "concordance_rate": 1.0,
            "extra_in_lumi": 0,
            "missing_in_lumi": 0,
        }

    ref_ts = set(ref_df["timestamp"].values) if not ref_df.empty else set()
    lumi_ts = set(lumi_df["timestamp"].values) if not lumi_df.empty else set()

    exact = ref_ts & lumi_ts
    missing = ref_ts - lumi_ts
    extra = lumi_ts - ref_ts
    total_union = len(exact) + len(missing) + len(extra)
    concordance = len(exact) / total_union if total_union > 0 else 1.0

    return {
        "ref_count": len(ref_ts),
        "lumi_count": len(lumi_ts),
        "exact_match": len(exact),
        "concordance_rate": concordance,
        "extra_in_lumi": len(extra),
        "missing_in_lumi": len(missing),
    }


def main() -> dict:
    print("=" * 60)
    print("Lumibot PoC — Signal Concordance Comparison")
    print("=" * 60)

    print("\n[1/4] Generating synthetic BTC 1-minute data...")
    m1 = generate_btc_1m(start="2025-01-01", end="2025-03-31", seed=42)
    h1 = add_trendpullback_features(resample_ohlcv(m1, "60min"))
    d1 = add_daily_trend_gate(resample_ohlcv(m1, "1D"))

    print(f"   M1 bars: {len(m1):,}")
    print(f"   H1 bars: {len(h1):,}")
    print(f"   D1 bars: {len(d1):,}")

    start, end = "2025-01-15", "2025-03-25"
    run_id = uuid.uuid4().hex[:12]

    print("\n[2/4] Extracting reference signals...")
    ref_signals = extract_reference(m1, h1, d1, start, end, run_id=run_id)
    ref_df = signals_to_df(ref_signals)
    print(f"   Reference signals: {len(ref_df)}")

    print("\n[3/4] Extracting Lumibot signals...")
    lumi_df = run_lumibot_signal_extraction(
        m1, h1, d1, start, end, run_id=run_id
    )
    print(f"   Lumibot signals:   {len(lumi_df)}")

    print("\n[4/4] Comparing signals...")
    result = compare_signals(ref_df, lumi_df)

    print(f"\n{'='*60}")
    print("RESULTS")
    print(f"{'='*60}")
    print(f"  Reference signals:   {result['ref_count']}")
    print(f"  Lumibot signals:     {result['lumi_count']}")
    print(f"  Exact matches:       {result['exact_match']}")
    print(f"  Concordance rate:    {result['concordance_rate']:.2%}")
    print(f"  Missing in Lumibot:  {result['missing_in_lumi']}")
    print(f"  Extra in Lumibot:    {result['extra_in_lumi']}")

    status = "PASS" if result["concordance_rate"] >= 0.95 else "FAIL"
    print(f"\n  Verdict: {status} (threshold: >= 95%)")
    print(f"{'='*60}")

    return result


if __name__ == "__main__":
    result = main()
    sys.exit(0 if result["concordance_rate"] >= 0.95 else 1)
