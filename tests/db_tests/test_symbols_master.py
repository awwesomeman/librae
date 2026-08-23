"""symbols instrument-master read/write — the DB's answer to what a bare
`symbol` string in a fact table actually means."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from librae.config.symbols import SymbolInfo
from librae.db.timescale_reader import load_symbols
from librae.db.timescale_writer import write_symbols


def _symbol(**overrides) -> SymbolInfo:
    fields = {
        "symbol": "BTCUSDT",
        "market": "crypto",
        "data_source": "binance",
        "instrument_type": "spot",
        "multiplier": 1.0,
        "data_adapter": "crypto",
        "venue_symbol": "BTC/USDT",
        "currency": "USDT",
    }
    fields.update(overrides)
    return SymbolInfo(**fields)


def test_schema_keys_the_master_on_the_fact_tables_triple() -> None:
    sql = Path("librae/db/timescale_init.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS symbols" in sql
    assert "PRIMARY KEY (symbol, data_source, instrument_type)" in sql
    # The domain must be declared before any table referencing it.
    assert sql.index("CREATE DOMAIN instrument_type_t") < sql.index(
        "CREATE TABLE IF NOT EXISTS symbols"
    )


@patch("librae.db.timescale_writer.psycopg2.extras.execute_values")
@patch("librae.db.timescale_writer.get_conn")
def test_write_symbols_upserts_on_the_natural_key(mock_get_conn, mock_exec_values) -> None:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    mock_get_conn.return_value = connection

    written = write_symbols(
        [_symbol(), _symbol(symbol="TXFR1", market="tw_futures", multiplier=200.0)]
    )

    assert written == 2
    sql = mock_exec_values.call_args[0][1]
    assert "ON CONFLICT (symbol, data_source, instrument_type) DO UPDATE" in sql
    rows = mock_exec_values.call_args[0][2]
    assert rows[0][:4] == ("BTCUSDT", "binance", "spot", "crypto")
    assert rows[1][4] == 200.0


@patch("librae.db.timescale_writer.get_conn")
def test_write_symbols_with_nothing_to_write_touches_no_connection(mock_get_conn) -> None:
    assert write_symbols([]) == 0
    mock_get_conn.assert_not_called()


@patch("librae.db.timescale_reader.pd.read_sql")
@patch("librae.db.timescale_reader.get_conn")
def test_load_symbols_filters_are_optional(mock_get_conn, mock_read_sql) -> None:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    mock_get_conn.return_value = connection

    load_symbols()
    assert "WHERE" not in mock_read_sql.call_args[0][0]

    load_symbols(market="crypto", instrument_type="contract_perpetual")
    sql, params = mock_read_sql.call_args[0][0], mock_read_sql.call_args[1]["params"]
    assert "market = %s" in sql and "instrument_type = %s" in sql
    assert params == ["crypto", "contract_perpetual"]


def test_load_symbols_rejects_an_unknown_instrument_type() -> None:
    with pytest.raises(ValueError, match="instrument_type"):
        load_symbols(instrument_type="option")
