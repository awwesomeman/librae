"""Every console page imports and renders without a database."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pandas as pd
import pytest
from librae.console import db, queries
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[2] / "librae" / "console" / "app.py"
PAGES = ("runs", "strategy", "signal")
SECTIONS = ("Performance", "Positions", "Runtime Events")


def _strategy_app(section: str = "Performance") -> AppTest:
    return AppTest.from_string(
        "import streamlit as st\n"
        "from librae.console import common\n"
        "from librae.console.views import strategy\n"
        f"st.session_state.setdefault(common.SECTION_KEY, {section!r})\n"
        "common.sidebar()\nstrategy.render()",
        default_timeout=30,
    )


def test_every_page_imports() -> None:
    for page in PAGES:
        assert callable(importlib.import_module(f"librae.console.views.{page}").render)


def test_the_entry_point_refuses_to_start_without_a_dsn(monkeypatch) -> None:
    monkeypatch.delenv(db.DSN_ENV, raising=False)
    app = AppTest.from_file(str(APP))
    app.run()

    assert not app.exception
    assert db.DSN_ENV in app.error[0].value


def _output_columns(sql: str) -> list[str]:
    """Names the outermost SELECT returns, enough to build an empty result."""
    if sql == queries.SIGNAL_RUNS:
        sql = queries.RUNS  # same output columns, with an EXISTS filter
    select = sql[sql.rindex("SELECT") + len("SELECT") : sql.rindex("FROM")]
    select = re.sub(r"DISTINCT ON \([^)]*\)", "", select)
    return [part.split()[-1].split(".")[-1] for part in select.split(",")]


@pytest.mark.parametrize("page", PAGES)
def test_a_page_over_an_empty_database_says_what_is_missing(page, monkeypatch) -> None:
    monkeypatch.setattr(
        db, "query", lambda sql, params=(): pd.DataFrame(columns=_output_columns(sql))
    )
    app = AppTest.from_string(
        f"""
from librae.console import common
from librae.console.views import {page}
common.sidebar(page={("overview" if page == "runs" else page)!r})
{page}.render()
"""
    )
    app.run()

    assert not app.exception
    assert app.info


@pytest.mark.parametrize(
    ("row", "expected"),
    [(1, "b"), (None, None), (2, None), (-1, None)],
)
def test_a_click_resolves_against_the_rows_on_screen(row, expected) -> None:
    from librae.console.views.runs import clicked_run

    assert clicked_run(row, ["a", "b"]) == expected


def test_a_name_click_requests_its_run_and_account() -> None:
    app = AppTest.from_string(
        """
import streamlit as st
from librae.console.views import runs

st.session_state["librae_runs_shown"] = ["run-a", "run-b"]
st.session_state["librae_runs_accounts_shown"] = ["account-a", "account-b"]
st.session_state["librae_strategy_click"] = {"row": 1}
runs._apply_click()
st.text(st.session_state["librae_pending_run_id"])
st.text(st.session_state["librae_pending_account_id"])
"""
    )
    app.run()

    assert not app.exception
    assert [t.value for t in app.text] == ["run-b", "account-b"]


def test_the_console_listens_on_localhost_unless_told_otherwise() -> None:
    from librae.console.__main__ import streamlit_argv

    default = streamlit_argv(["--server.port", "8501"], {})
    assert default[3:] == ["--server.address", "127.0.0.1", "--server.port", "8501"]
    chosen = streamlit_argv(["--server.address=100.64.0.1"], {})
    assert "127.0.0.1" not in chosen
    assert "127.0.0.1" not in streamlit_argv([], {"STREAMLIT_SERVER_ADDRESS": "100.64.0.1"})


@pytest.mark.parametrize("section", SECTIONS)
def test_strategy_sections_keep_account_data_and_charts_available(section, monkeypatch) -> None:
    from tests.console.sample_data import query

    monkeypatch.setattr(db, "query", query)
    app = _strategy_app(section).run()
    assert not app.exception
    assert app.title
    assert app.dataframe or app.get("plotly_chart")
    if section == "Performance":
        assert app.metric[0].value == "113,200.00"
        app.selectbox(key="librae_account_id").select("USD-secondary").run()
        assert not app.exception
        assert app.metric[0].value == "25,000.00"


def test_strategy_and_mode_only_offer_their_own_runs(monkeypatch) -> None:
    from librae.console import common

    from tests.console.sample_data import query

    monkeypatch.setattr(db, "query", query)
    app = _strategy_app().run()
    assert app.session_state[common.STRATEGY_KEY] == "Momentum Trend"
    assert len(app.selectbox(key=common.RUN_KEY).options) == 2
    app.selectbox(key=common.MODE_KEY).select("backtest").run()
    assert app.session_state[common.RUN_KEY] == "run-trend-backtest"
    assert len(app.selectbox(key=common.RUN_KEY).options) == 1
    app.selectbox(key=common.STRATEGY_KEY).select("Funding Carry").run()
    assert not app.exception
    assert app.title[0].value == "Funding Carry"
    assert app.session_state[common.MODE_KEY] == common.ALL_MODES
    assert app.session_state[common.RUN_KEY] == "run-carry"
    assert len(app.selectbox(key=common.RUN_KEY).options) == 1
    assert "halted" in app.error[0].value


def test_switching_sections_preserves_run_and_account_scope(monkeypatch) -> None:
    from librae.console import common, queries

    from tests.console.sample_data import query

    calls = []

    def record(sql, params=()):
        calls.append((sql, params))
        return query(sql, params)

    monkeypatch.setattr(db, "query", record)
    app = _strategy_app().run()
    app.selectbox(key=common.RUN_KEY).select("run-trend-backtest").run()
    app.selectbox(key=common.ACCOUNT_KEY).select("USD-secondary").run()
    calls.clear()
    app.radio(key=common.SECTION_KEY).set_value("Runtime Events").run()
    assert not app.exception
    assert common.ACCOUNT_KEY not in [widget.key for widget in app.selectbox]
    assert app.session_state[common.RUN_KEY] == "run-trend-backtest"
    assert not any(
        sql in (queries.PERFORMANCE, queries.EQUITY, queries.OPEN_POSITIONS) for sql, _ in calls
    )
    assert (queries.RUNTIME_EVENTS, ("run-trend-backtest", queries.RECENT_EVENTS)) in calls
    app.radio(key=common.SECTION_KEY).set_value("Positions").run()
    assert not app.exception
    assert app.selectbox(key=common.ACCOUNT_KEY).value == "USD-secondary"
    assert (queries.OPEN_POSITIONS, ("run-trend-backtest", "USD-secondary")) in calls


def test_signal_only_run_opens_analysis_without_an_account(monkeypatch) -> None:
    from librae.console import common

    from tests.console.sample_data import query

    monkeypatch.setattr(db, "query", query)
    app = AppTest.from_string(
        "from librae.console import common\n"
        "from librae.console.views import signal\n"
        "common.sidebar(page='signal')\nsignal.render()",
        default_timeout=30,
    ).run()
    app.selectbox(key=common.SIGNAL_NAME_KEY).select("Signal Lab").run()
    assert not app.exception
    assert app.title[0].value == "Signal"
    assert app.session_state[common.SIGNAL_RUN_KEY] == "run-signals"
    assert app.get("plotly_chart")
    assert common.ACCOUNT_KEY not in [widget.key for widget in app.selectbox]


def test_signal_analysis_keeps_its_own_selection_and_never_reads_accounts(monkeypatch) -> None:
    from librae.console import common

    from tests.console.sample_data import query

    calls = []

    def record(sql, params=()):
        calls.append(sql)
        return query(sql, params)

    monkeypatch.setattr(db, "query", record)
    app = AppTest.from_string(
        "import streamlit as st\n"
        "from librae.console import common\n"
        "from librae.console.views import signal, strategy, runs\n"
        "page = st.radio('Page', ['strategy', 'signal', 'overview'], key='test_page')\n"
        "common.sidebar(page=page)\n"
        "{'strategy': strategy, 'signal': signal, 'overview': runs}[page].render()",
        default_timeout=30,
    ).run()
    app.selectbox(key=common.RUN_KEY).select("run-trend-backtest").run()
    app.selectbox(key=common.ACCOUNT_KEY).select("USD-secondary").run()
    calls.clear()
    app.radio(key="test_page").set_value("signal").run()
    app.selectbox(key=common.SIGNAL_NAME_KEY).select("Signal Lab").run()
    assert not app.exception
    assert queries.RUN_ACCOUNTS not in calls
    assert queries.PERFORMANCE not in calls
    assert queries.SIGNAL_SYMBOLS in calls
    assert app.session_state[common.RUN_KEY] == "run-trend-backtest"
    app.radio(key="test_page").set_value("overview").run()
    app.radio(key="test_page").set_value("strategy").run()
    assert not app.exception
    assert app.selectbox(key=common.RUN_KEY).value == "run-trend-backtest"
    assert app.selectbox(key=common.ACCOUNT_KEY).value == "USD-secondary"
    app.radio(key="test_page").set_value("signal").run()
    assert app.selectbox(key=common.SIGNAL_NAME_KEY).value == "Signal Lab"


def test_run_directory_keeps_signal_only_runs(monkeypatch) -> None:
    from tests.console.sample_data import query

    monkeypatch.setattr(db, "query", query)
    app = AppTest.from_string("from librae.console.views import runs\nruns.render()").run()
    assert not app.exception
    assert "run-signals" in app.dataframe[0].value["run_id"].tolist()
    assert app.metric[0].value == "6"


def test_overview_distinguishes_missing_historical_and_delayed_equity(monkeypatch) -> None:
    from librae.console import queries

    from tests.console.sample_data import query

    def mixed_freshness(sql, params=()):
        frame = query(sql, params)
        if sql == queries.ACCOUNT_OVERVIEW:
            frame.loc[frame["run_id"] == "run-carry", "last_heartbeat_at"] = pd.NaT
            frame.loc[frame["run_id"] == "run-breakout", "last_equity_at"] -= pd.Timedelta(hours=4)
        return frame

    monkeypatch.setattr(db, "query", mixed_freshness)
    app = AppTest.from_string("from librae.console.views import runs\nruns.render()").run()
    assert not app.exception
    assert app.title[0].value == "Overview"
    table = app.dataframe[0].value.set_index("run_id")
    assert table["freshness"].to_dict() == {
        "run-trend": "Current",
        "run-signals": "No Data",
        "run-carry": "Not Monitored",
        "run-breakout": "Delayed",
        "run-backtest": "Historical",
        "run-trend-backtest": "Historical",
    }
    assert "behind" not in table.columns
    assert not app.dataframe[0].proto.selection_mode


def test_run_request_replaces_an_unrelated_strategy_mode_and_account(monkeypatch) -> None:
    from tests.console.sample_data import query

    monkeypatch.setattr(db, "query", query)
    app = AppTest.from_string(
        "import streamlit as st\n"
        "from librae.console import common\n"
        "st.session_state[common.STRATEGY_KEY] = 'Momentum Trend'\n"
        "st.session_state[common.MODE_KEY] = 'backtest'\n"
        "common.request_run('run-carry', 'USD-secondary')\n"
        "common.sidebar()\n"
        "selected = common.account_selector(common.selection())\n"
        "st.text(selected.run['strategy_name'])\n"
        "st.text(selected.run['run_id'])\n"
        "st.text(selected.account_id)\n"
    ).run()
    assert not app.exception
    assert [item.value for item in app.text] == ["Funding Carry", "run-carry", "USD-secondary"]


def test_identical_run_labels_still_expose_distinct_ids(monkeypatch) -> None:
    from librae.console import common, queries

    from tests.console.sample_data import query

    def same_time(sql, params=()):
        frame = query(sql, params)
        if sql == queries.RUNS:
            mask = frame["strategy_name"] == "Momentum Trend"
            frame.loc[mask, "run_at"] = frame.loc[mask, "run_at"].iloc[0]
            frame.loc[mask, "mode"] = "sim"
        return frame

    monkeypatch.setattr(db, "query", same_time)
    app = _strategy_app().run()
    labels = app.selectbox(key=common.RUN_KEY).options
    assert len(set(labels)) == 2
    assert labels[0].endswith("run-trend")
    assert labels[1].endswith("run-trend-backtest")


def test_requesting_signal_analysis_sets_its_run_without_an_account(monkeypatch) -> None:
    from librae.console import common

    from tests.console.sample_data import query

    monkeypatch.setattr(db, "query", query)
    app = AppTest.from_string(
        "from librae.console import common\n"
        "from librae.console.views import signal\n"
        "common.request_signal('run-signals')\n"
        "common.sidebar(page='signal')\nsignal.render()",
        default_timeout=30,
    ).run()
    app.run()
    assert not app.exception
    assert app.selectbox(key=common.SIGNAL_RUN_KEY).value == "run-signals"
    assert common.ACCOUNT_KEY not in [widget.key for widget in app.selectbox]


def test_compact_status_keeps_halt_warning_and_actual_heartbeat_limit(monkeypatch) -> None:
    from tests.console.sample_data import query

    monkeypatch.setattr(db, "query", query)
    app = AppTest.from_string(
        "from librae.console import common\ncommon.status_panel('run-carry', 'Funding Carry')"
    ).run()
    assert not app.exception
    assert "halted" in app.error[0].value
    text = " ".join(element.proto.body for element in app.get("html"))
    assert "90 seconds" in text and "Run ID: run-carry" in text
    assert "Halted" in text
    assert not app.metric


def test_tooltip_escapes_database_names() -> None:
    from librae.console.ui import hint

    html = hint('<script>alert("x")</script>', "Run ID: <untrusted>")
    assert "<script>" not in html and "<untrusted>" not in html
    assert "&lt;untrusted&gt;" in html


def test_launcher_applies_theme_without_overwriting_explicit_settings(monkeypatch) -> None:
    from librae.console import __main__ as launcher

    for option in launcher.THEME_DEFAULTS:
        monkeypatch.delenv(option, raising=False)
    monkeypatch.delenv("STREAMLIT_CLIENT_TOOLBAR_MODE", raising=False)
    monkeypatch.setattr(launcher.sys, "argv", ["console", "--server.port", "8512"])
    monkeypatch.setenv("STREAMLIT_THEME_PRIMARY_COLOR", "#123456")
    monkeypatch.delenv("STREAMLIT_THEME_BACKGROUND_COLOR", raising=False)
    monkeypatch.setattr(launcher.cli, "main", lambda: 0)
    with pytest.raises(SystemExit) as result:
        launcher.main()
    assert result.value.code == 0
    assert launcher.os.environ["STREAMLIT_THEME_PRIMARY_COLOR"] == "#123456"
    assert launcher.os.environ["STREAMLIT_THEME_BACKGROUND_COLOR"] == "#f7f9fb"
