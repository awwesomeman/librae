"""Monitoring totals, refresh behaviour and account boundaries without a database."""

from xml.etree.ElementTree import fromstring

import pandas as pd
import pytest
from librae.console import db, queries
from streamlit.testing.v1 import AppTest

from tests.console.sample_data import query
from tests.console.test_pages import _strategy_app


@pytest.mark.parametrize("case", ["flat", "missing_mark", "missing_margin"])
def test_account_snapshot_distinguishes_flat_from_incomplete(case, monkeypatch) -> None:
    def read(sql, params=()):
        frame = query(sql, params)
        if sql == queries.OPEN_POSITIONS:
            if case == "flat":
                return frame.iloc[:0]
            if case == "missing_margin":
                frame.loc[0, "margin_locked"] = float("nan")
        if sql == queries.LATEST_MARKS and case == "missing_mark":
            return frame.iloc[:1]
        return frame

    monkeypatch.setattr(db, "query", read)
    app = AppTest.from_string(
        "from librae.console.views.performance import _account_now\n"
        "_account_now('run-trend', 'USD-main', 'USD', 'H1', 30)",
        default_timeout=30,
    ).run()
    assert not app.exception
    metrics = {m.label: m for m in app.metric}
    if case == "flat":
        assert metrics["Unrealized P&L"].value == "0.00"
        assert metrics["Margin Locked"].value == "0.0%"
        assert metrics["Margin Locked"].delta == "0.00 USD"
    elif case == "missing_mark":
        assert metrics["Unrealized P&L"].value == "–"
        assert metrics["Margin Locked"].value == "16.6%"
    else:
        assert metrics["Margin Locked"].value == "–"
        assert metrics["Unrealized P&L"].value != "–"


def test_drawdown_uses_full_run_aggregate_and_keeps_realized_basis(monkeypatch) -> None:
    calls = []

    def read(sql, params=()):
        if sql == queries.EQUITY_DRAWDOWN:
            calls.append(params)
            return pd.DataFrame({"max_drawdown": [-0.20], "max_drawdown_amount": [-24000.0]})
        return query(sql, params)

    monkeypatch.setattr(db, "query", read)
    app = _strategy_app().run()
    assert not app.exception
    table = next(
        item.proto.body for item in app.get("html") if 'class="results-grid"' in item.proto.body
    )
    rows = {
        row.find("./th//button").text: " ".join(row.find("./td").itertext())
        for row in fromstring(table).findall(".//tbody/tr")
    }
    assert rows["Max Equity Drawdown"] == "-20.00% -24,000.00 USD"
    assert rows["Realized Max Drawdown"] == "-3.40%"
    assert rows["Realized Return"] == "11.25% 11,250.00 USD"
    assert rows["Trades"] == "128"
    assert rows["Period Sharpe"] == "1.84"
    assert rows["Period Sortino"] == "2.31"
    assert rows["Win Rate"] == "58.6%"
    assert rows["Profit Factor"] == "1.72"
    app.selectbox(key="librae_account_id").select("USD-secondary").run()
    assert calls[-1] == ("run-trend", "USD-secondary")


def test_signal_snapshot_reads_new_prices_without_loading_history(monkeypatch) -> None:
    price = 100.0
    calls = []
    reference_ts = pd.Timestamp("2026-01-02", tz="UTC")

    def read(sql, params=()):
        calls.append(sql)
        if sql == queries.SIGNAL_SUMMARY:
            return pd.DataFrame(
                {
                    "signal_count": [2],
                    "last_ts": [reference_ts - pd.Timedelta(hours=1)],
                    "last_value": [1.0],
                }
            )
        if sql == queries.SIGNAL_REFERENCE:
            return pd.DataFrame({"ts": [reference_ts], "close": [100.0]})
        if sql == queries.LATEST_MARKS:
            return pd.DataFrame({"symbol": ["BTCUSDT"], "ts": [reference_ts], "close": [price]})
        raise AssertionError("Snapshot must not load history or account data")

    monkeypatch.setattr(db, "query", read)
    app = AppTest.from_string(
        "from librae.console.views.signals import _snapshot\n"
        "_snapshot('run-signals', 'entry', 'BTCUSDT', 'long', 'close')",
        default_timeout=30,
    ).run()
    assert not app.exception
    assert app.metric[-1].value == "0.00%"
    price = 110
    app.run()
    assert not app.exception
    assert app.metric[-1].value == "10.00%"
    assert len(app.metric) == 4


def test_manual_history_refresh_keeps_selection_and_clears_query_cache(monkeypatch) -> None:
    cleared = []
    calls = []

    def read(sql, params=()):
        calls.append(sql)
        return query(sql, params)

    monkeypatch.setattr(db, "query", read)
    monkeypatch.setattr(db, "clear_query_cache", lambda: cleared.append(True))
    app = _strategy_app().run()
    app.selectbox(key="librae_account_id").select("USD-secondary").run()
    calls.clear()
    app.button(key="librae_performance_refresh").click().run()
    assert not app.exception
    assert cleared == [True]
    assert queries.LATEST_EQUITY in calls
    assert queries.PERFORMANCE in calls
    assert queries.EQUITY_DRAWDOWN in calls
    assert app.selectbox(key="librae_account_id").value == "USD-secondary"
    assert app.metric[0].value == "25,000.00"
