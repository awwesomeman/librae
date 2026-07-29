"""Persistence contracts for perpetual-funding diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from db.timescale_writer import save_backtest_output, write_funding_cash_flow
from librae.backtest.schema import (
    AccountPerformance,
    BacktestOutput,
    FundingCashFlowRecord,
    RunMetadata,
    StrategyMetrics,
)


def test_schema_defines_idempotent_funding_event_key() -> None:
    sql = Path("db/timescale_init.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS funding_cash_flows" in sql
    assert "ON funding_cash_flows(run_id, account_id, symbol, ts)" in sql
    assert "REFERENCES backtest_runs(run_id) ON DELETE CASCADE" in sql


@patch("db.timescale_writer.get_conn")
def test_write_funding_cash_flow_upserts_same_payment(mock_get_conn: MagicMock) -> None:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    mock_get_conn.return_value = connection
    ts = datetime(2026, 1, 1, tzinfo=UTC)

    write_funding_cash_flow(
        run_id="run-1",
        account_id="perp",
        currency="USDT",
        ts=ts,
        symbol="BTC/USDT:USDT",
        side="long",
        quantity=2.0,
        mark_price=100_000.0,
        multiplier=1.0,
        rate=0.0001,
        cash_flow=-20.0,
    )

    sql, values = cursor.execute.call_args.args
    assert "ON CONFLICT (run_id, account_id, symbol, ts) DO UPDATE" in sql
    assert values == (
        ts,
        "run-1",
        "perp",
        "USDT",
        "BTC/USDT:USDT",
        "long",
        2.0,
        100_000.0,
        1.0,
        0.0001,
        -20.0,
    )


@patch("db.timescale_writer.write_run_metadata")
@patch("db.timescale_writer._claim_config_hash", return_value=True)
@patch("db.timescale_writer.psycopg2.extras.execute_values")
@patch("db.timescale_writer.get_conn")
def test_save_backtest_output_batches_funding_diagnostics(
    mock_get_conn: MagicMock,
    mock_execute_values: MagicMock,
    _mock_claim: MagicMock,
    _mock_metadata: MagicMock,
) -> None:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    mock_get_conn.return_value = connection
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    output = BacktestOutput(
        run_metadata=RunMetadata(
            run_id="funding-20260101t0000-abcdef",
            strategy="funding",
            symbols=("BTC/USDT:USDT",),
            timeframe="H1",
            data_source="test",
            started_at=ts,
            ended_at=ts,
            run_at=ts,
        ),
        accounts=(
            AccountPerformance(
                account_id="perp",
                currency="USDT",
                initial_cash=1_000.0,
                final_equity=980.0,
                net_pnl=-20.0,
                equity_curve=(),
                metrics=StrategyMetrics(total_return=-0.02),
            ),
        ),
        order_events=(),
        position_snapshots=(),
        allocation_snapshots=(),
        funding_cash_flows=(
            FundingCashFlowRecord(
                ts=ts,
                account_id="perp",
                currency="USDT",
                symbol="BTC/USDT:USDT",
                side="long",
                quantity=2.0,
                mark_price=100_000.0,
                multiplier=1.0,
                rate=0.0001,
                cash_flow=-20.0,
            ),
        ),
    )

    counts = save_backtest_output(output)

    assert counts["funding_cash_flows"] == 1
    funding_call = next(
        call
        for call in mock_execute_values.call_args_list
        if "INSERT INTO funding_cash_flows" in call.args[1]
    )
    assert funding_call.args[2][0][-1] == -20.0
