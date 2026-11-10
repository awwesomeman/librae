"""Run selection, the run status panel, and helpers shared by the pages.

The selectors render in the entry point's sidebar, which Streamlit keeps
across page switches; pages read the choice back from ``st.session_state``.
Status panels are fragments that rerun every ``REFRESH_SECONDS`` on their
own, so a refresh re-reads the status and leaves the rest of the page alone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pandas as pd
import streamlit as st

from librae.console import db, queries

REFRESH_ENV = "LIBRAE_CONSOLE_REFRESH_SECONDS"
REFRESH_SECONDS = int(os.environ.get(REFRESH_ENV) or 30)
if REFRESH_SECONDS <= 0:
    raise ValueError(f"{REFRESH_ENV} must be a positive number of seconds")

RUN_KEY = "librae_run_id"
_PENDING_RUN_KEY = "librae_pending_run_id"
ACCOUNT_KEY = "librae_account_id"
MODE_KEY = "librae_mode"
ALL_MODES = "all"


@dataclass(frozen=True)
class Selection:
    run: pd.Series  # one row of queries.RUNS
    account_id: str | None
    currency: str | None


def _keep_valid(key: str, options: list[str]) -> None:
    """Fall back to the first option when the remembered value is no longer offered."""
    if st.session_state.get(key) not in options:
        st.session_state[key] = options[0] if options else None


def sidebar() -> None:
    """Render the run and account selectors in the sidebar."""
    runs = db.query(queries.RUNS, (queries.RECENT_RUNS,))
    pending = st.session_state.pop(_PENDING_RUN_KEY, None)
    if pending is not None:
        st.session_state[MODE_KEY] = ALL_MODES
        st.session_state[RUN_KEY] = pending
    with st.sidebar:
        modes = [ALL_MODES, *sorted(runs["mode"].dropna().unique())]
        _keep_valid(MODE_KEY, modes)
        mode = st.segmented_control("Mode", modes, key=MODE_KEY) or ALL_MODES
        shown = runs if mode == ALL_MODES else runs[runs["mode"] == mode]
        run_ids = shown["run_id"].tolist()
        labels = {
            row.run_id: f"{row.strategy_name} · {row.mode} · {row.run_at:%Y-%m-%d %H:%M}"
            for row in shown.itertuples()
        }
        _keep_valid(RUN_KEY, run_ids)
        st.selectbox(
            "Run", run_ids, key=RUN_KEY, format_func=labels.get, placeholder="No runs recorded"
        )
        accounts = selected_accounts()
        _keep_valid(ACCOUNT_KEY, accounts["account_id"].tolist())
        if len(accounts) > 1:
            st.selectbox("Account", accounts["account_id"].tolist(), key=ACCOUNT_KEY)
        st.caption(f"Status panels refresh every {REFRESH_SECONDS} s.")


def selected_accounts() -> pd.DataFrame:
    run_id = st.session_state.get(RUN_KEY)
    if run_id is None:
        return pd.DataFrame(columns=["account_id", "currency"])
    return db.query(queries.RUN_ACCOUNTS, (run_id,))


def request_run(run_id: str) -> None:
    """Ask for ``run_id`` to become the sidebar's run on the next full rerun.

    The choice is parked under a key no widget owns and applied before the
    sidebar renders: a fragment cannot redraw the sidebar, and a widget's own
    key cannot change once that widget has rendered.
    """
    if st.session_state.get(RUN_KEY) != run_id:
        st.session_state[_PENDING_RUN_KEY] = run_id


def run_requested() -> bool:
    return _PENDING_RUN_KEY in st.session_state


def selection() -> Selection | None:
    """The sidebar's choice, or None after telling the reader there is no run."""
    run_id = st.session_state.get(RUN_KEY)
    runs = db.query(queries.RUNS, (queries.RECENT_RUNS,))
    match = runs[runs["run_id"] == run_id]
    if match.empty:
        st.info("No run is recorded in this database yet. Runs appear once a strategy saves one.")
        return None
    account_id = st.session_state.get(ACCOUNT_KEY)
    accounts = selected_accounts()
    currency = accounts.loc[accounts["account_id"] == account_id, "currency"]
    return Selection(
        run=match.iloc[0],
        account_id=account_id,
        currency=None if currency.empty else str(currency.iloc[0]),
    )


def now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def as_of(ts: object, *, behind: bool = False, what: str = "The newest data") -> None:
    """Caption a panel with the time of its newest data, warning when that is late."""
    if ts is None or pd.isna(ts):
        st.caption("No data yet.")
        return
    stamp = pd.Timestamp(ts)
    st.caption(
        f"Data as of {stamp:%Y-%m-%d %H:%M:%S} UTC "
        f"({queries.format_age(now() - stamp)} ago) · checked {now():%H:%M:%S} UTC"
    )
    if behind:
        st.warning(
            f"{what} is older than two bars plus three poll cycles: the run may have "
            "stalled (a market closure also explains it).",
            icon=":material/schedule:",
        )


@st.fragment(run_every=REFRESH_SECONDS)
def status_panel(run_id: str) -> None:
    """Heartbeat, halt flag and last decision of one run."""
    state = db.query(queries.RUN_STATE, (run_id,))
    if state.empty:
        return
    row = state.iloc[0]
    current = now()
    status = queries.run_status(row["last_heartbeat_at"], row["poll_seconds"], current)
    st.caption(f"`{run_id}` · {row['mode']} · {row['timeframe']}")
    if row["mode"] == "backtest":
        st.caption("Backtest: no runtime status.")
        return
    cols = st.columns(4)
    cols[0].metric(
        "Status",
        status,
        help="running: heartbeat within three poll cycles; stale: heartbeats missed.",
    )
    halted = None if pd.isna(row["halted"]) else bool(row["halted"])
    cols[1].metric(
        "Halted",
        "unknown" if halted is None else ("yes" if halted else "no"),
        help="From the run's latest checkpoint.",
    )
    heartbeat = row["last_heartbeat_at"]
    cols[2].metric(
        "Heartbeat",
        "never" if pd.isna(heartbeat) else f"{queries.format_age(current - heartbeat)} ago",
    )
    decision = row["last_decision_bar"]
    cols[3].metric(
        "Last decision bar",
        "none" if pd.isna(decision) else f"{pd.Timestamp(decision):%m-%d %H:%M}",
        help="Start of the last bar the run made a decision on, from its checkpoint.",
    )
    if halted:
        st.error("The run is halted: it places no new entries until an operator resets it.")
    as_of(
        decision,
        behind=queries.is_behind(decision, row["timeframe"], row["poll_seconds"], current),
        what="The last decision bar",
    )


def missing_bars(mode: str) -> str:
    """Why a run has no stored bars: only a backtest can be saved without them."""
    if mode == "backtest":
        return "A backtest stores its bars only when saved with store_bars=True."
    return "A sim or live run stores the bars behind its decisions; none are recorded here."


def history(sql: str, params: tuple) -> pd.DataFrame:
    """The newest ``HISTORY_ROWS`` rows of a run in time order, saying so when that cut it."""
    frame = queries.oldest_first(db.query(sql, (*params, queries.HISTORY_ROWS)))
    if len(frame) == queries.HISTORY_ROWS:
        st.caption(f"Showing the latest {queries.HISTORY_ROWS:,} rows of this run.")
    return frame


def recent(bars: pd.DataFrame) -> pd.DataFrame:
    """The last bars, as many as the reader asks for; a full run can be too many to draw."""
    if len(bars) <= 50:
        return bars
    count = st.number_input(
        "Bars shown", min_value=50, max_value=len(bars), value=min(1000, len(bars)), step=50
    )
    return bars.tail(int(count))
