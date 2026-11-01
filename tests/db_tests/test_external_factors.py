"""Tests for external_factors / external_factor_coverage_ranges read/write —
the generic third-party-factor cache (funding rate, open interest, ...).
Mirrors test_ohlcv_coverage.py + write_ohlcv's coverage since it's the same
DB-first + gap-tracked-cache design, generalized to (symbol, factor_name,
timeframe, data_source) instead of (symbol, timeframe, data_source)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from librae.db.timescale_reader import get_external_factor_coverage_ranges, load_external_factor
from librae.db.timescale_writer import (
    ExternalFactorWrite,
    merge_external_factor_coverage_ranges,
    write_external_factor,
)


def _mock_conn(mock_cur: MagicMock) -> MagicMock:
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cur
    return mock_conn


class TestGetExternalFactorCoverage:
    @patch("librae.db.timescale_reader.get_conn")
    def test_returns_sorted_ranges(self, mock_conn_ctx):
        mock_cur = MagicMock()
        r1 = (datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))
        mock_cur.fetchall.return_value = [r1]
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        result = get_external_factor_coverage_ranges("BTCUSDT", "funding_rate", "H8", "binanceusdm")

        assert result == [r1]
        sql = mock_cur.execute.call_args[0][0]
        assert "external_factor_coverage_ranges" in sql


class TestMergeExternalFactorCoverage:
    @patch("librae.db.timescale_writer.psycopg2.extras.execute_values")
    @patch("librae.db.timescale_writer.get_conn")
    def test_merges_touching_ranges(self, mock_conn_ctx, mock_exec_values):
        mock_cur = MagicMock()
        existing = (1, datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))
        mock_cur.fetchall.return_value = [existing]
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        merge_external_factor_coverage_ranges(
            "BTCUSDT",
            "funding_rate",
            "H8",
            "binanceusdm",
            datetime(2024, 1, 2, tzinfo=UTC),
            datetime(2024, 1, 3, tzinfo=UTC),
        )

        delete_calls = [c for c in mock_cur.execute.call_args_list if "DELETE" in c[0][0]]
        assert len(delete_calls) == 1
        assert delete_calls[0][0][1] == ([1],)

        inserted_rows = mock_exec_values.call_args[0][2]
        assert len(inserted_rows) == 1
        assert inserted_rows[0][5] == datetime(2024, 1, 1, tzinfo=UTC)
        assert inserted_rows[0][6] == datetime(2024, 1, 3, tzinfo=UTC)

    @patch("librae.db.timescale_writer.psycopg2.extras.execute_values")
    @patch("librae.db.timescale_writer.get_conn")
    def test_keeps_disjoint_ranges_separate(self, mock_conn_ctx, mock_exec_values):
        mock_cur = MagicMock()
        existing = (1, datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))
        mock_cur.fetchall.return_value = [existing]
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        merge_external_factor_coverage_ranges(
            "BTCUSDT",
            "funding_rate",
            "H8",
            "binanceusdm",
            datetime(2024, 1, 10, tzinfo=UTC),
            datetime(2024, 1, 11, tzinfo=UTC),
        )

        inserted_rows = mock_exec_values.call_args[0][2]
        assert len(inserted_rows) == 2

    def test_rejects_invalid_instrument_type(self):
        with pytest.raises(ValueError, match="instrument_type"):
            merge_external_factor_coverage_ranges(
                "BTCUSDT",
                "funding_rate",
                "H8",
                "binanceusdm",
                datetime(2024, 1, 2, tzinfo=UTC),
                datetime(2024, 1, 3, tzinfo=UTC),
                instrument_type="daily",
            )


def _write_cursor(*, inserted: int = 0, revised: int = 0) -> MagicMock:
    cur = MagicMock()
    cur.fetchone.return_value = (inserted,)
    cur.rowcount = revised
    return cur


def _staged_csv(cur: MagicMock) -> str:
    return cur.copy_expert.call_args[0][1].getvalue()


def _executed(cur: MagicMock) -> list[str]:
    return [" ".join(c[0][0].split()) for c in cur.execute.call_args_list]


class TestWriteExternalFactor:
    def test_empty_df_writes_nothing(self):
        assert write_external_factor(
            pd.DataFrame(), "BTCUSDT", "funding_rate", "H8", "binanceusdm"
        ) == ExternalFactorWrite(inserted=0, revised=0)

    def test_naive_timestamp_raises(self):
        df = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01"]), "value": [0.01]})

        with pytest.raises(ValueError, match="timezone-naive"):
            write_external_factor(df, "BTCUSDT", "funding_rate", "H8", "binanceusdm")

    def test_naive_vintage_raises(self):
        df = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"]),
                "value": [0.01],
                "vintage_at": pd.to_datetime(["2024-02-01"]),
            }
        )

        with pytest.raises(ValueError, match="vintage_at is timezone-naive"):
            write_external_factor(df, "US", "gdp", "Q1", "alfred")

    def test_rejects_invalid_instrument_type(self):
        df = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"]), "value": [0.01]})

        with pytest.raises(ValueError, match="instrument_type"):
            write_external_factor(
                df, "BTCUSDT", "funding_rate", "H8", "binanceusdm", instrument_type="daily"
            )

    @pytest.mark.parametrize(
        ("vintages", "match"),
        [
            (None, "more than once without vintage_at"),
            (["2024-02-01T00:00:00Z", None], "more than once without vintage_at"),
            (["2024-02-01T00:00:00Z", "2024-02-01T00:00:00Z"], "repeats the same vintage_at"),
        ],
    )
    def test_a_repeated_timestamp_needs_distinct_vintages(self, vintages, match):
        """Repeated observations are versions, and only vintages order them."""
        df = pd.DataFrame(
            {"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"] * 2), "value": [1.0, 2.0]}
        )
        if vintages is not None:
            df["vintage_at"] = pd.to_datetime(vintages, utc=True)

        with pytest.raises(ValueError, match=match):
            write_external_factor(df, "US", "gdp", "Q1", "alfred")

    @patch("librae.db.timescale_writer.get_conn")
    def test_stages_naive_utc_rows_and_reports_counts(self, mock_conn_ctx):
        cur = _write_cursor(inserted=1, revised=2)
        mock_conn_ctx.return_value = _mock_conn(cur)
        df = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2020-01-01T09:30:00+08:00"] * 3),
                "value": [1.0, 2.0, float("nan")],
                "vintage_at": pd.to_datetime(
                    ["2020-04-29T00:00:00Z", "2020-05-28T00:00:00Z", "2020-06-25T00:00:00Z"]
                ),
            }
        )

        result = write_external_factor(df, "US", "gdp", "Q1", "alfred")

        assert result == ExternalFactorWrite(inserted=1, revised=2)
        # COPY reads naive text in the session zone, so the writer pins it to UTC.
        assert "SET LOCAL TIME ZONE 'UTC'" in _executed(cur)
        assert _staged_csv(cur).splitlines() == [
            "2020-01-01 01:30:00,1.0,2020-04-29",
            "2020-01-01 01:30:00,2.0,2020-05-28",
            "2020-01-01 01:30:00,NaN,2020-06-25",
        ]

    @patch("librae.db.timescale_writer.get_conn")
    def test_rows_without_vintage_stage_a_null_vintage(self, mock_conn_ctx):
        cur = _write_cursor(inserted=2)
        mock_conn_ctx.return_value = _mock_conn(cur)
        df = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-01-01T00:00:00Z", "2024-01-01T08:00:00Z"]),
                "value": [0.0001, -0.0002],
            }
        )

        result = write_external_factor(df, "BTC/USDT:USDT", "funding_rate", "H8", "binanceusdm")

        assert result == ExternalFactorWrite(inserted=2, revised=0)
        assert _staged_csv(cur).splitlines() == [
            "2024-01-01 00:00:00,0.0001,",
            "2024-01-01 08:00:00,-0.0002,",
        ]

    @patch("librae.db.timescale_writer.get_conn")
    def test_archives_before_it_overwrites_under_one_key_lock(self, mock_conn_ctx):
        cur = _write_cursor()
        mock_conn_ctx.return_value = _mock_conn(cur)
        df = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"]), "value": [1.0]})

        write_external_factor(df, "BTCUSDT", "funding_rate", "H8", "binanceusdm")

        statements = _executed(cur)
        assert statements[0].startswith("SELECT pg_advisory_xact_lock")
        lock_key = cur.execute.call_args_list[0][0][1][0]
        assert lock_key.split("\x1f")[1:] == [
            "BTCUSDT",
            "funding_rate",
            "H8",
            "binanceusdm",
            "spot",
        ]
        archive = next(i for i, s in enumerate(statements) if "INTO external_factors_history" in s)
        upsert = next(i for i, s in enumerate(statements) if "INTO external_factors (" in s)
        assert archive < upsert


class TestLoadExternalFactor:
    @patch("librae.db.timescale_reader.get_conn")
    def test_returns_tz_aware_frame(self, mock_conn_ctx):
        mock_conn_ctx.return_value.__enter__.return_value = MagicMock()
        with patch("librae.db.timescale_reader.pd.read_sql") as mock_read_sql:
            mock_read_sql.return_value = pd.DataFrame(
                {
                    "timestamp": pd.to_datetime(["2024-01-01T00:00:00"]),
                    "value": [0.0001],
                }
            )
            result = load_external_factor("BTCUSDT", "open_interest", "H1", "data.binance.vision")

        assert result["timestamp"].dt.tz is not None

    @patch("librae.db.timescale_reader.get_conn")
    def test_without_as_of_reads_only_current_rows(self, mock_conn_ctx):
        mock_conn_ctx.return_value.__enter__.return_value = MagicMock()
        with patch("librae.db.timescale_reader.pd.read_sql") as mock_read_sql:
            mock_read_sql.return_value = pd.DataFrame(columns=["timestamp", "value"])
            load_external_factor("BTCUSDT", "open_interest", "H1", "data.binance.vision")

        sql = mock_read_sql.call_args[0][0]
        assert "external_factors_history" not in sql
        assert "SELECT ts AS timestamp, value FROM external_factors" in sql

    def test_as_of_must_be_timezone_aware(self):
        with pytest.raises(ValueError, match="as_of must be timezone-aware"):
            load_external_factor("US", "gdp", "Q1", "alfred", as_of="2024-01-01")

    @staticmethod
    def _load_as_of(as_of, *, earliest, without_vintage=1, **kwargs):
        cur = MagicMock()
        cur.fetchone.return_value = (earliest, without_vintage)
        conn = MagicMock()
        conn.cursor.return_value = cur
        with (
            patch("librae.db.timescale_reader.get_conn") as mock_conn_ctx,
            patch("librae.db.timescale_reader.pd.read_sql") as mock_read_sql,
        ):
            mock_conn_ctx.return_value.__enter__.return_value = conn
            mock_read_sql.return_value = pd.DataFrame(
                {
                    "timestamp": [datetime(2020, 1, 1, tzinfo=UTC)],
                    "value": [1.0],
                    "recorded_at": [None],
                    "vintage_at": [datetime(2020, 4, 29, tzinfo=UTC)],
                }
            )
            result = load_external_factor("US", "gdp", "Q1", "alfred", as_of=as_of, **kwargs)
        return result, mock_read_sql.call_args

    def test_as_of_reads_every_version_and_returns_both_clocks(self, caplog):
        result, call = self._load_as_of(
            "2021-01-01T00:00:00Z",
            earliest=datetime(2020, 4, 29, tzinfo=UTC),
            started_at="2020-01-01",
        )

        assert list(result.columns) == ["timestamp", "value", "recorded_at", "vintage_at"]
        assert all(result[c].dt.tz is not None for c in ("timestamp", "recorded_at", "vintage_at"))
        sql, params = call[0][0], call[1]["params"]
        assert "external_factors_history" in sql
        assert "COALESCE(vintage_at, recorded_at)" in sql
        assert sql.count("ts >= %(started_at)s") == 2  # both tables honour the range
        assert params["as_of"] == datetime(2021, 1, 1, tzinfo=UTC)
        assert "precedes every recorded_at" not in caplog.text

    @pytest.mark.parametrize(
        "earliest", [datetime(2022, 1, 1, tzinfo=UTC), None], ids=["before", "untracked"]
    )
    def test_as_of_before_any_recorded_time_warns(self, earliest, caplog):
        """A result that cannot be point-in-time must not look like one."""
        self._load_as_of("2021-01-01T00:00:00Z", earliest=earliest)

        assert "precedes every recorded_at and vintage_at" in caplog.text

    def test_a_factor_with_only_vintages_does_not_warn(self, caplog):
        """Every row is then dropped or exact, so nothing comes back stale."""
        self._load_as_of("2021-01-01T00:00:00Z", earliest=None, without_vintage=0)

        assert "precedes every recorded_at" not in caplog.text
