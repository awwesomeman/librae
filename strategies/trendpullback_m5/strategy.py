"""TrendPullback M5 — same logic as TrendPullback, lower timeframe for testing.

M30 trend gate + M5 entry/exit. Reuses TrendPullbackStrategy directly.
"""
from strategies.trendpullback.strategy import TrendPullbackStrategy

# WHY: Same on_bar logic (entry_signal/exit_signal/max_hold_periods).
# Only the timeframe and data pipeline differ (handled in utils.py + run.py).
TrendPullbackM5Strategy = TrendPullbackStrategy
