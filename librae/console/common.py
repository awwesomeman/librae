"""Run selection, the run status panel, and helpers shared by the pages.

The selectors render in the entry point's sidebar, which Streamlit keeps
across page switches; pages read the choice back from ``st.session_state``.
Status panels are fragments that rerun every ``REFRESH_SECONDS`` on their
own, so a refresh re-reads the status and leaves the rest of the page alone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from html import escape
from typing import Literal

import pandas as pd
import streamlit as st

from librae.console import db, queries, ui
from librae.core.run_config import HEARTBEAT_STALE_AFTER_POLLS

REFRESH_ENV = "LIBRAE_CONSOLE_REFRESH_SECONDS"
REFRESH_SECONDS = int(os.environ.get(REFRESH_ENV) or 30)
HISTORY_REFRESH_SECONDS = 300
if REFRESH_SECONDS <= 0:
    raise ValueError(f"{REFRESH_ENV} must be a positive number of seconds")

RUN_KEY = "librae_run_id"
_PENDING_RUN_KEY = "librae_pending_run_id"
_PENDING_ACCOUNT_KEY = "librae_pending_account_id"
ACCOUNT_KEY = "librae_account_id"
MODE_KEY = "librae_mode"
STRATEGY_KEY = "librae_strategy_name"
SECTION_KEY = "librae_strategy_section"
SIGNAL_NAME_KEY = "librae_signal_analysis"
SIGNAL_MODE_KEY = "librae_signal_mode"
SIGNAL_RUN_KEY = "librae_signal_run_id"
_PENDING_SIGNAL_KEY = "librae_pending_signal_run_id"
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


def sidebar(*, page: Literal["overview", "strategy", "signal"] = "strategy") -> None:
    """Choose a strategy and one of its runs; keep the context across pages."""
    # Detach context from widget cleanup when Overview or a run-wide section hides it.
    for key in (
        STRATEGY_KEY,
        MODE_KEY,
        RUN_KEY,
        ACCOUNT_KEY,
        SECTION_KEY,
        SIGNAL_NAME_KEY,
        SIGNAL_MODE_KEY,
        SIGNAL_RUN_KEY,
    ):
        if key in st.session_state:
            st.session_state[key] = st.session_state[key]
    if page in ("strategy", "signal"):
        _run_selectors(signal=page == "signal")
    with st.sidebar:
        st.html(
            '<div class="sidebar-foot"><strong>Read-only monitoring</strong><br>'
            f"Status refreshes every {REFRESH_SECONDS} s<br>Times shown in UTC</div>"
        )


def _run_selectors(*, signal: bool = False) -> None:
    sql = queries.SIGNAL_RUNS if signal else queries.RUNS
    name_key, mode_key, run_key = (
        (SIGNAL_NAME_KEY, SIGNAL_MODE_KEY, SIGNAL_RUN_KEY)
        if signal
        else (STRATEGY_KEY, MODE_KEY, RUN_KEY)
    )
    runs = db.query(sql, (queries.RECENT_RUNS,))
    pending = st.session_state.pop(_PENDING_SIGNAL_KEY if signal else _PENDING_RUN_KEY, None)
    if pending is not None:
        match = runs[runs["run_id"] == pending]
        if not match.empty:
            st.session_state[name_key] = match.iloc[0]["strategy_name"]
        else:
            st.warning(
                "The selected run is no longer in the recent run list. Showing the latest run."
            )
        st.session_state[mode_key] = ALL_MODES
        st.session_state[run_key] = pending
        if not signal:
            st.session_state[SECTION_KEY] = None
            st.session_state[ACCOUNT_KEY] = st.session_state.pop(_PENDING_ACCOUNT_KEY, None)
    with st.sidebar:
        st.html(
            f'<div class="sidebar-section">{"Signal" if signal else "Strategy"} Selection</div>'
        )
        # Most recently recorded strategy first, consistent with the initial run.
        strategies = runs["strategy_name"].drop_duplicates().tolist()
        _keep_valid(name_key, strategies)
        strategy = st.selectbox(
            "Analysis" if signal else "Strategy",
            strategies,
            key=name_key,
            placeholder="No signals recorded" if signal else "No strategies recorded",
            help=(
                f"Saved names in the latest {queries.RECENT_RUNS} runs with signals. "
                "Includes standalone analyses and signals from strategy runs."
                if signal
                else f"Strategies in the latest {queries.RECENT_RUNS} recorded runs. "
                "The name groups runs; results always belong to one Run ID."
            ),
        )
        strategy_runs = runs[runs["strategy_name"] == strategy]
        modes = [ALL_MODES, *sorted(strategy_runs["mode"].dropna().unique())]
        _keep_valid(mode_key, modes)
        mode = st.selectbox("Mode", modes, key=mode_key, format_func=str.title) or ALL_MODES
        shown = strategy_runs if mode == ALL_MODES else strategy_runs[strategy_runs["mode"] == mode]
        run_ids = shown["run_id"].tolist()
        labels = {
            row.run_id: f"{row.run_at:%Y-%m-%d %H:%M:%S} · {row.mode.title()}"
            for row in shown.itertuples()
        }
        duplicate_labels = pd.Series(labels, dtype=str).duplicated(keep=False)
        for run_id in duplicate_labels[duplicate_labels].index:
            labels[run_id] += f" · {run_id}"
        _keep_valid(run_key, run_ids)
        st.selectbox(
            "Run", run_ids, key=run_key, format_func=labels.get, placeholder="No runs recorded"
        )


def selected_accounts() -> pd.DataFrame:
    run_id = st.session_state.get(RUN_KEY)
    if run_id is None:
        return pd.DataFrame(columns=["account_id", "currency"])
    return db.query(queries.RUN_ACCOUNTS, (run_id,))


def request_run(run_id: str, account_id: str | None = None) -> None:
    """Ask for ``run_id`` to become the sidebar's run on the next full rerun.

    The choice is parked under a key no widget owns and applied before the
    sidebar renders: a fragment cannot redraw the sidebar, and a widget's own
    key cannot change once that widget has rendered.
    """
    st.session_state[_PENDING_RUN_KEY] = run_id
    st.session_state[_PENDING_ACCOUNT_KEY] = account_id


def run_requested() -> bool:
    return _PENDING_RUN_KEY in st.session_state


def request_signal(run_id: str) -> None:
    """Open a run's raw signals without changing the strategy's account context."""
    st.session_state[_PENDING_SIGNAL_KEY] = run_id


def signal_requested() -> bool:
    return _PENDING_SIGNAL_KEY in st.session_state


def signal_selection() -> Selection | None:
    runs = db.query(queries.SIGNAL_RUNS, (queries.RECENT_RUNS,))
    match = runs[runs["run_id"] == st.session_state.get(SIGNAL_RUN_KEY)]
    if match.empty:
        st.info("No saved signal runs yet. Save signals with a Run ID to inspect them here.")
        return None
    return Selection(match.iloc[0], None, None)


def selection() -> Selection | None:
    """The sidebar's choice, or None after telling the reader there is no run."""
    run_id = st.session_state.get(RUN_KEY)
    runs = db.query(queries.RUNS, (queries.RECENT_RUNS,))
    match = runs[runs["run_id"] == run_id]
    if match.empty:
        st.info("No run is recorded in this database yet. Runs appear once a strategy saves one.")
        return None
    accounts = selected_accounts()
    _keep_valid(ACCOUNT_KEY, accounts["account_id"].tolist())
    account_id = st.session_state.get(ACCOUNT_KEY)
    currency = accounts.loc[accounts["account_id"] == account_id, "currency"]
    return Selection(
        run=match.iloc[0],
        account_id=account_id,
        currency=None if currency.empty else str(currency.iloc[0]),
    )


def account_selector(selected: Selection) -> Selection:
    """Account scope applies to performance and positions, never signals or events."""
    accounts = selected_accounts()
    if accounts.empty:
        return selected
    labels = {row.account_id: f"{row.account_id} · {row.currency}" for row in accounts.itertuples()}
    account_id = st.selectbox(
        "Account",
        accounts["account_id"].tolist(),
        key=ACCOUNT_KEY,
        format_func=labels.get,
        disabled=len(accounts) == 1,
        help="Performance and positions for this account only; balances are never combined.",
    )
    currency = accounts.loc[accounts["account_id"] == account_id, "currency"].iloc[0]
    return Selection(selected.run, account_id, str(currency))


def now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def as_of(ts: object, *, behind: bool = False, what: str = "The newest data") -> None:
    """Caption a panel with the time of its newest data, warning when that is late."""
    if ts is None or pd.isna(ts):
        st.caption("No data yet.")
        return
    stamp = pd.Timestamp(ts)
    current = now()
    st.html(
        '<div class="data-timestamp">'
        + ui.hint(
            f"Latest Data {queries.format_age(current - stamp)} ago",
            f"Newest data timestamp: {stamp:%Y-%m-%d %H:%M:%S} UTC.",
        )
        + f"<span>Checked {current:%H:%M:%S} UTC</span></div>"
    )
    if behind:
        st.warning(
            f"{what} is older than two bars plus three poll cycles: the run may have "
            "stalled (a market closure also explains it).",
            icon=":material/schedule:",
        )


@st.fragment(run_every=REFRESH_SECONDS)
def status_panel(run_id: str, label: str) -> None:
    """Heartbeat, halt flag and last decision of one run."""
    state = db.query(queries.RUN_STATE, (run_id,))
    if state.empty:
        return
    row = state.iloc[0]
    current = now()
    status = queries.run_status(row["last_heartbeat_at"], row["poll_seconds"], current)
    identity = (
        '<span class="run-name">' + ui.hint(label, f"Run ID: {run_id}") + "</span>"
        f"<span>{escape(str(row['mode']).title())} · {escape(str(row['timeframe']))}</span>"
    )
    if row["mode"] == "backtest":
        st.html(f'<div class="run-context">{identity}<span>No runtime heartbeat</span></div>')
        return
    halted = None if pd.isna(row["halted"]) else bool(row["halted"])
    poll = row["poll_seconds"]
    limit = (
        f"{float(poll) * HEARTBEAT_STALE_AFTER_POLLS:g} seconds"
        if pd.notna(poll) and poll > 0
        else "an unavailable threshold"
    )
    explanation = {
        "running": f"Heartbeat received within {limit} ({HEARTBEAT_STALE_AFTER_POLLS} poll cycles).",
        "stale": f"No heartbeat for more than {limit} ({HEARTBEAT_STALE_AFTER_POLLS} poll cycles).",
        "not polling": "No heartbeat or valid polling interval is recorded for this run.",
    }[status]
    if halted is False:
        explanation += " The checkpoint is not halted."
    heartbeat = row["last_heartbeat_at"]
    decision = row["last_decision_bar"]
    heartbeat_label = (
        "No heartbeat"
        if pd.isna(heartbeat)
        else f"Heartbeat {queries.format_age(current - heartbeat)} ago"
    )
    decision_label = (
        "No decision bar" if pd.isna(decision) else f"Decision {pd.Timestamp(decision):%m-%d %H:%M}"
    )
    state_class = "halted" if halted else status.replace(" ", "-")
    st.html(
        '<div class="run-context"><div class="run-context-main">'
        + identity
        + f'<span class="run-state state-{state_class}">'
        + ui.hint(
            "Halted" if halted else status.title(),
            "The checkpoint is halted; no new entries are placed." if halted else explanation,
        )
        + "</span>"
        + (
            ui.hint("Halt Unknown", "No halt flag is recorded in the latest checkpoint.")
            if halted is None
            else ""
        )
        + '</div><div class="run-context-details">'
        + ui.hint(
            heartbeat_label,
            explanation
            + (f" Last received: {heartbeat}." if pd.notna(heartbeat) else "")
            + f" Checked {current:%H:%M:%S} UTC.",
        )
        + ui.hint(
            decision_label,
            "No decision bar is recorded in the checkpoint."
            if pd.isna(decision)
            else f"Start of the last decision bar in the checkpoint: {decision}.",
        )
        + "</div></div>"
    )
    if halted:
        st.error("The run is halted: it places no new entries until an operator resets it.")
    if queries.is_behind(decision, row["timeframe"], row["poll_seconds"], current):
        st.warning(
            "The last decision bar is older than two bars plus three poll cycles. "
            "The run may have stalled, or its market may be closed.",
            icon=":material/schedule:",
        )


def missing_bars(mode: str) -> str:
    """Why a run has no stored bars: only a backtest can be saved without them."""
    if mode == "backtest":
        return "A backtest stores its bars only when saved with store_bars=True."
    return "A sim or live run stores the bars behind its decisions; none are recorded here."


def refresh_toolbar(
    ts: object, page: Literal["performance", "positions", "signal"], *, what: str
) -> None:
    """Keep freshness and whole-page refresh together without resetting selections."""
    checked = now()
    has_data = ts is not None and pd.notna(ts)
    freshness = (
        f"Latest Data {queries.format_age(checked - pd.Timestamp(ts))} ago"
        if has_data
        else "No Data Yet"
    )
    timestamp = (
        f"{what}: {pd.Timestamp(ts):%Y-%m-%d %H:%M:%S} UTC. "
        if has_data
        else f"{what}: not recorded. "
    )
    with st.container(
        horizontal=True,
        horizontal_alignment="distribute",
        vertical_alignment="center",
        key=f"librae_{page}_toolbar",
    ):
        st.html(
            '<div class="data-timestamp">'
            + ui.hint(freshness, timestamp + f"Snapshot checked {checked:%H:%M:%S} UTC.")
            + ui.hint(
                "Auto Refresh",
                f"Latest snapshot: every {REFRESH_SECONDS} seconds. "
                "Historical results and charts: every 5 minutes. "
                "Refresh updates the whole page immediately.",
            )
            + "</div>",
            width="content",
        )
        if st.button(
            "Refresh",
            icon=":material/refresh:",
            type="tertiary",
            key=f"librae_{page}_refresh",
        ):
            db.clear_query_cache()
            st.rerun()


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
        "Bars Shown", min_value=50, max_value=len(bars), value=min(1000, len(bars)), step=50
    )
    return bars.tail(int(count))
