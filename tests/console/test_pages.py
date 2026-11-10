"""Every console page imports and renders without a database."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pandas as pd
import pytest
from librae.console import db
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[2] / "librae" / "console" / "app.py"
PAGES = ("runs", "performance", "positions", "signals", "events")


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
common.sidebar()
{page}.render()
"""
    )
    app.run()

    assert not app.exception
    assert app.info


@pytest.mark.parametrize(
    ("rows", "expected"),
    [([1], "b"), ([], None), ([2], None)],  # [2]: the row a filter or refresh removed
)
def test_a_click_resolves_against_the_rows_on_screen(rows, expected) -> None:
    from librae.console.views.runs import clicked_run

    assert clicked_run(rows, ["a", "b"]) == expected


def test_a_click_requests_its_run_and_clears_the_table_selection() -> None:
    """The selection is a row position; kept, it would later name another run."""
    app = AppTest.from_string(
        """
from types import SimpleNamespace
import streamlit as st
from librae.console.views import runs

st.session_state["librae_runs_shown"] = ["run-a", "run-b"]
before = runs._table_key()
st.session_state[before] = SimpleNamespace(selection=SimpleNamespace(rows=[1]))
runs._apply_click()
st.text(st.session_state["librae_pending_run_id"])
st.text(str(runs._table_key() != before))
"""
    )
    app.run()

    assert not app.exception
    assert [t.value for t in app.text] == ["run-b", "True"]


def test_the_console_listens_on_localhost_unless_told_otherwise() -> None:
    from librae.console.__main__ import streamlit_argv

    default = streamlit_argv(["--server.port", "8501"], {})
    assert default[3:] == ["--server.address", "127.0.0.1", "--server.port", "8501"]
    chosen = streamlit_argv(["--server.address=100.64.0.1"], {})
    assert "127.0.0.1" not in chosen
    assert "127.0.0.1" not in streamlit_argv([], {"STREAMLIT_SERVER_ADDRESS": "100.64.0.1"})
