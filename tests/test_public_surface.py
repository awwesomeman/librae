"""The import paths applications are told to use keep resolving.

The supported surface is declared in architecture.md (Public import surface).
``DOWNSTREAM_IMPORTS`` lists what applications import from it today, so
renaming or dropping one of those names is a breaking change made on purpose,
not by accident.
"""

from __future__ import annotations

import importlib

import librae
import pytest

DOWNSTREAM_IMPORTS = {
    "librae": (
        "AccountConfig",
        "AccountPerformance",
        "AccountSnapshot",
        "Backtest",
        "BacktestOutput",
        "Context",
        "CostModel",
        "EquityCurvePoint",
        "ExecutionPolicy",
        "FinancingCashFlow",
        "FinancingCashFlowRecord",
        "FinancingLifecycleEvent",
        "MarketDataSubscription",
        "OrderIntent",
        "PortfolioWeights",
        "Position",
        "PositionEventRecord",
        "REASON_FORCE_CLOSE",
        "RunConfig",
        "RunMetadata",
        "Strategy",
        "StrategyMetrics",
        "TAIFEX_INDEX_CALENDAR",
        "attach_borrow_rate",
        "attach_funding_rate",
        "attribute_financing_to_closes",
        "compute_trade_entry_outcomes",
        "compute_trade_lifecycle_outcomes",
        "generate_run_id",
        "interval_to_timedelta",
        "next_segment_open_after",
        "period_close",
        "period_start",
        "resolve_symbol",
        "session_label",
        "session_labels",
        "summarize_trade_entry_outcomes",
        "summarize_trade_lifecycle_outcomes",
        "taifex_final_settlement_day",
        "to_canonical",
        "to_ccxt",
        "validate_session_mode",
    ),
    "librae.testing": ("validate_feature_causality",),
    "librae.config.env": ("RedactSecrets", "SECRET_NAMES", "doctor"),
    "librae.brokers.shioaji_time": ("fold_closing_minutes", "shioaji_kbar_starts"),
    "librae.db": ("get_conn",),
    "librae.db.timescale_reader": (
        "load_backtest_output",
        "load_equity_curve",
        "load_financing_cash_flows",
        "load_performance",
        "load_position_events",
        "load_run_bars",
        "row_to_strategy_metrics",
    ),
    "librae.db.timescale_writer": (
        "save_backtest_output",
        "save_signal_results",
        "save_strategy_results",
    ),
    "librae.orchestration.cli": ("RunOptions", "build_run", "run_dispatch", "run_realtime_generic"),
}


@pytest.mark.parametrize("module_name", sorted(DOWNSTREAM_IMPORTS))
def test_downstream_imports_resolve(module_name: str) -> None:
    module = importlib.import_module(module_name)
    missing = [name for name in DOWNSTREAM_IMPORTS[module_name] if not hasattr(module, name)]
    assert missing == []


def test_top_level_names_are_exported() -> None:
    assert set(DOWNSTREAM_IMPORTS["librae"]) <= set(librae.__all__)
    assert [name for name in librae.__all__ if not hasattr(librae, name)] == []
