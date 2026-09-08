#!/usr/bin/env python3
"""Grafana dashboard generator.

Produces the checked-in Strategy, Account Overview, and Signal dashboards.
Usage: python -m librae.app.grafana.generate_dashboards
"""

from __future__ import annotations

import copy
import json
import logging
import pathlib

logger = logging.getLogger(__name__)

# WHY: restated, not imported. The deploy venv installs what the dashboard
# push needs, not the engine, and importing anything under `librae` pulls in
# librae/__init__.py and its numpy dependency. tests/deploy pins this value
# against the runtime constant instead.
HEARTBEAT_STALE_AFTER_POLLS = 3

DATASOURCE: dict = {"type": "grafana-postgresql-datasource", "uid": "P40AE60E18F02DE32"}
OUT_DIR: pathlib.Path = pathlib.Path(__file__).parent / "provisioning" / "dashboards" / "json"


def _target(sql: str, ref_id: str = "A", fmt: str = "time_series") -> dict:
    return {"rawSql": sql, "format": fmt, "refId": ref_id, "datasource": DATASOURCE}


def _color_override(name: str, color: str) -> dict:
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [
            {"id": "color", "value": {"fixedColor": color, "mode": "fixed"}},
        ],
    }


# Who sets margin_rate (see librae/config/market_config.py's MarginMode) —
# shared by every panel that surfaces the Margin Mode field, so the same
# financing regime always reads as the same color. Trade Events/Position
# Snapshot show Margin Mode as plain text (consistent with their other enum
# columns, e.g. Event/Side); only Margin Locked by Mode's bar gauge colors it.
_MARGIN_MODE_COLORS = {"unlevered": "text", "fixed": "blue", "dynamic": "orange"}


def _width_override(name: str, px: int) -> dict:
    """Fixed pixel width for a table column, sized to its actual content
    (e.g. 'reduce'/'short' vs. a full timestamp) instead of Grafana's
    equal-split default."""
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [{"id": "custom.width", "value": px}],
    }


def _sign_color_mappings() -> list[dict]:
    """Value mappings that color a formatted text field (e.g. "-500 / -12%")
    red/green by its leading sign — for stat panels combining $ and % into
    one string, where Grafana's numeric thresholds no longer apply."""
    return [
        {"type": "regex", "options": {"pattern": "^-", "result": {"color": "red"}}},
        {"type": "regex", "options": {"pattern": "^[^-]", "result": {"color": "green"}}},
    ]


def _stat_target(sql: str) -> dict:
    return _target(sql, "A", "table")


def _stat_panel(
    title: str,
    sql: str,
    unit: str | None,
    thresholds: list[dict],
    *,
    layout: str = "kpi",
    w: int = 4,
    description: str | None = None,
    decimals: int | None = None,
    no_value: str | None = None,
    fixed_color: str | None = None,
) -> dict:
    """Build a Grafana stat panel definition."""
    defaults: dict = {
        "thresholds": {"mode": "absolute", "steps": thresholds},
    }
    if fixed_color:
        defaults["color"] = {"fixedColor": fixed_color, "mode": "fixed"}
    else:
        defaults["color"] = {"mode": "thresholds"}
    if unit:
        defaults["unit"] = unit
    if decimals is not None:
        defaults["decimals"] = decimals
    if no_value:
        defaults["noValue"] = no_value

    panel: dict = {
        "_type": layout,
        "title": title,
        "type": "stat",
        "h": 4,
        "w": w,
        "targets": [_stat_target(sql)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "colorMode": "value",
            "graphMode": "none",
        },
    }
    if description:
        panel["description"] = description
    return panel


def _account_metric_sql(column: str) -> str:
    return (
        f"SELECT {column} FROM strategy_performance"
        " WHERE run_id = '${run_id}' AND account_id IN (${account_id:sqlstring})"
    )


def _data_source_filter(meta_alias: str, ohlcv_alias: str) -> str:
    # data_source='multi' means the run mixes sources (e.g. spot+perp); treat it
    # like NULL (unrestricted) rather than a literal value ohlcv.data_source can match.
    return (
        f"({meta_alias}.data_source IS NULL OR {meta_alias}.data_source = 'multi'"
        f" OR {ohlcv_alias}.data_source = {meta_alias}.data_source)"
    )


def _latest_position_event_order(alias: str = "") -> str:
    """Return the canonical newest-position ordering for ``DISTINCT ON``.

    Event ids zero-pad only the first four sequence digits, so length must
    precede lexical order once a run reaches its ten-thousandth event.
    """
    prefix = f"{alias}." if alias else ""
    return (
        f"ORDER BY {prefix}symbol, {prefix}ts DESC, "
        f"length({prefix}event_id) DESC, {prefix}event_id DESC"
    )


def _runtime_status_case(alias: str = "") -> str:
    """Return health status from the persisted runtime polling contract."""
    prefix = f"{alias}." if alias else ""
    heartbeat = f"{prefix}last_heartbeat_at"
    poll_seconds = f"{prefix}poll_seconds"
    return (
        "CASE"
        f" WHEN {heartbeat} IS NULL OR {poll_seconds} IS NULL OR {poll_seconds} <= 0 THEN -1"
        f" WHEN {heartbeat} >= now() - make_interval("
        f"secs => {poll_seconds} * {HEARTBEAT_STALE_AFTER_POLLS}) THEN 1"
        " ELSE 0"
        " END"
    )


def _status_value_mappings() -> list[dict]:
    """Return consistent Grafana labels and colors for runtime health."""
    return [
        {
            "type": "value",
            "options": {
                "1": {"text": "Online", "color": "green", "index": 0},
                "0": {"text": "Offline", "color": "red", "index": 1},
                "-1": {"text": "-", "color": "text", "index": 2},
            },
        }
    ]


# Returns integer for Grafana value mapping: 1=Online, 0=Offline, -1=N/A.
_STATUS_SQL = (
    f"SELECT {_runtime_status_case()} AS status FROM backtest_runs WHERE run_id = '${{run_id}}'"
)

STATUS_PANEL: dict = {
    "_type": "kpi",
    "title": "Status",
    "description": (
        f"Online if the last heartbeat is within {HEARTBEAT_STALE_AFTER_POLLS} polling cycles. "
        "Offline means the process may have stopped."
    ),
    "type": "stat",
    "h": 4,
    "w": 4,
    "targets": [_stat_target(_STATUS_SQL)],
    "fieldConfig": {
        "defaults": {
            "mappings": _status_value_mappings(),
            "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
            "color": {"mode": "fixed"},
        },
        "overrides": [],
    },
    "options": {
        "reduceOptions": {"calcs": ["lastNotNull"]},
        "colorMode": "background",
        "graphMode": "none",
    },
}


def _poll_seconds_panel(w: int) -> dict:
    return _stat_panel(
        "Poll Seconds",
        "SELECT poll_seconds AS \"Seconds\" FROM backtest_runs WHERE run_id = '${run_id}'",
        "s",
        [],
        w=w,
        fixed_color="blue",
        no_value="N/A",
        description="Polling interval in seconds. Only set for sim/live runs.",
    )


# ---------------------------------------------------------------------------
# KPI catalogue — all available stat panels for the Overview row.
# Edit DEFAULT_KPIS to control which KPIs appear on the dashboard.
# ---------------------------------------------------------------------------

# All of strategy_performance (every KPI below) is realized-only: it's
# recomputed and written when a trade closes/reduces or funding accrues
# (see live/engine.py's _performance_dirty), never on open/add and never
# per-bar. Unrealized P&L is the one number on this dashboard computed live
# on every query — the two are expected to move independently, most visibly
# right after opening a first position, where Unrealized P&L already
# reflects the live mark while every KPI here still reads the pre-trade
# baseline (see register_run's seeded $0/0.0% row) until the first close.
_REALIZED_CADENCE_NOTE = " Realized-only: updates on close/reduce or funding, not every bar."

_KPI_CATALOGUE: dict[str, dict] = {
    "total_return": {
        "_type": "kpi",
        "title": "Total Return",
        "description": (
            "Compounded return over the full stored sample, shown as "
            '"$ / %". Not annualized. Net of cost.' + _REALIZED_CADENCE_NOTE
        ),
        "type": "stat",
        "h": 4,
        "w": 4,
        "targets": [
            _stat_target(
                "SELECT ROUND(net_pnl::numeric,0)::text || ' / ' ||\n"
                "  ROUND((total_return*100)::numeric,1)::text || '%' AS \"Total Return\"\n"
                "FROM strategy_performance\n"
                "WHERE run_id = '${run_id}' AND account_id IN (${account_id:sqlstring})"
            )
        ],
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
                "mappings": _sign_color_mappings(),
            },
            "overrides": [],
        },
        "options": {
            # fields:"" (the default) means "numeric fields only" — this
            # panel's value is a formatted text field ("6500 / 6.5%"), which
            # that default silently excludes, showing "No data" even though
            # the query returns a row. "/.*/" includes it.
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/"},
            "colorMode": "value",
            "graphMode": "none",
            "justifyMode": "center",
        },
    },
    "max_drawdown": {
        "_type": "kpi",
        "title": "Max Drawdown",
        "description": (
            'Largest peak-to-trough decline, shown as "$ / %". Always ≤ 0. '
            "The $ figure is live (equity_curve, updated every bar, includes "
            "unrealized swings); the % figure is realized-only (strategy_"
            "performance, updates on close/reduce/funding) — the two can "
            "diverge before the first close."
        ),
        "type": "stat",
        "h": 4,
        "w": 4,
        "targets": [
            _stat_target(
                "WITH curve AS (\n"
                "  SELECT equity, MAX(equity) OVER (ORDER BY ts) AS peak\n"
                "  FROM equity_curve\n"
                "  WHERE run_id = '${run_id}' AND account_id IN (${account_id:sqlstring})\n"
                "),\n"
                "dd AS (\n"
                "  SELECT MAX(peak - equity) AS dollar_dd FROM curve\n"
                ")\n"
                "SELECT ROUND((-dd.dollar_dd)::numeric,0)::text || ' / ' ||\n"
                "  ROUND((sp.max_drawdown*100)::numeric,1)::text || '%' AS \"Max Drawdown\"\n"
                "FROM dd CROSS JOIN strategy_performance sp\n"
                "WHERE sp.run_id = '${run_id}' AND sp.account_id IN (${account_id:sqlstring})"
            )
        ],
        "fieldConfig": {
            "defaults": {
                "color": {"fixedColor": "red", "mode": "fixed"},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/"},
            "colorMode": "value",
            "graphMode": "none",
            "justifyMode": "center",
        },
    },
    "period_sharpe": _stat_panel(
        "Period Sharpe",
        _account_metric_sql("period_sharpe"),
        None,
        [
            {"color": "red", "value": None},
            {"color": "green", "value": 0},
        ],
        description=(
            "Mean period return / sample period volatility. Not annualized; "
            "compare only like-frequency observations. Net of cost." + _REALIZED_CADENCE_NOTE
        ),
    ),
    "period_sortino": _stat_panel(
        "Period Sortino",
        _account_metric_sql("period_sortino"),
        None,
        [
            {"color": "red", "value": None},
            {"color": "green", "value": 0},
        ],
        description=(
            "Mean period return / period downside deviation. Not annualized. Net of cost."
            + _REALIZED_CADENCE_NOTE
        ),
    ),
    "win_rate": _stat_panel(
        "Win Rate",
        _account_metric_sql("win_rate"),
        "percentunit",
        [{"color": "red", "value": None}, {"color": "green", "value": 0.5}],
        description=(
            "Winning trades (net_pnl > 0) / total trades. Read together with "
            "Profit Factor — low win rate + high profit factor means trend "
            "following." + _REALIZED_CADENCE_NOTE
        ),
    ),
    "profit_factor": _stat_panel(
        "Profit Factor",
        _account_metric_sql("profit_factor"),
        None,
        [
            {"color": "red", "value": None},
            {"color": "yellow", "value": 1.0},
            {"color": "green", "value": 1.5},
        ],
        description=(
            "Sum of winning net P&L / sum of losing net P&L. Blank if there are "
            "no losing trades yet. >1.5 healthy, <1.0 losing." + _REALIZED_CADENCE_NOTE
        ),
    ),
    "trades": _stat_panel(
        "Trades",
        _account_metric_sql("trades"),
        None,
        [{"color": "blue", "value": None}],
        description="Total closed trades (reduce + close). Low count (<30) = metrics statistically unreliable.",
    ),
    "avg_trade_return": _stat_panel(
        "Avg Trade Return",
        _account_metric_sql("avg_trade_return"),
        "percentunit",
        [{"color": "red", "value": None}, {"color": "green", "value": 0}],
        description="Notional-weighted mean return per realized exit. Net of cost.",
    ),
    "exposure_ratio": _stat_panel(
        "Exposure",
        _account_metric_sql("exposure_ratio"),
        "percentunit",
        [{"color": "blue", "value": None}],
        description="Bars with any open position / total bars. Multi-asset safe — overlapping positions counted once.",
    ),
    "mean_period_return": _stat_panel(
        "Mean Period Return",
        _account_metric_sql("mean_period_return"),
        "percentunit",
        [{"color": "red", "value": None}, {"color": "green", "value": 0}],
        description="Arithmetic mean return per stored observation. Not annualized. Net of cost.",
    ),
    "positive_period_rate": _stat_panel(
        "Positive Periods",
        _account_metric_sql("positive_period_rate"),
        "percentunit",
        [{"color": "blue", "value": None}],
        description="Observations with period_return > 0 divided by all observations.",
    ),
}

# WHY: edit this list to change which KPIs appear on the dashboard.
# All metrics are always computed and stored in DB — this only controls display.
DEFAULT_KPIS: list[str] = [
    "total_return",
    "max_drawdown",
    "period_sharpe",
    "period_sortino",
    "win_rate",
    "profit_factor",
]

_OPEN_POSITIONS_PANEL = _stat_panel(
    "Open Positions",
    (
        'SELECT COUNT(*) AS "Count" FROM (\n'
        "  SELECT DISTINCT ON (symbol) symbol, remaining_quantity\n"
        "  FROM position_events\n"
        "  WHERE run_id = '${run_id}' AND account_id IN (${account_id:sqlstring})\n"
        f"  {_latest_position_event_order()}\n"
        ") p\n"
        "WHERE p.remaining_quantity > 0"
    ),
    None,
    [{"color": "blue", "value": None}],
    decimals=0,
    description="Distinct symbols currently held. Pairs with Trades (closed count) next to it.",
)

BASE_PANELS_DEF: list[dict] = [
    {"_type": "row", "title": "Performance Overview"},
    # Row 1: is it alive, what is it doing right now — the monitoring
    # question this dashboard exists to answer moment to moment, so it
    # gets the top-left, most-scanned position. Status leads: "is the
    # process even running" beats every other number if the answer is no.
    STATUS_PANEL,
    _poll_seconds_panel(w=4),
    _OPEN_POSITIONS_PANEL,
    _KPI_CATALOGUE["trades"],
    {
        "_type": "kpi",
        "title": "Unrealized P&L",
        "description": (
            'Mark-to-market P&L across all open positions, shown as "$ / % of '
            "equity\" — same figure as Position Snapshot's MTM P&L footer, "
            "computed live on every query. Total Return picks this up only as of "
            "its last refresh (close/reduce/funding, not per-bar — see its own "
            "note); between refreshes the two can diverge by exactly today's "
            "unrealized move, not double-counted profit. Backtest runs mark "
            "against the run's own ended_at, not a live price, so a finished "
            "run's number won't drift."
        ),
        "type": "stat",
        "h": 4,
        "w": 4,
        "targets": [
            _stat_target(
                "WITH meta AS ("
                " SELECT timeframe, data_source, mode, ended_at"
                " FROM backtest_runs WHERE run_id='${run_id}'"
                "),\n"
                "positions AS (\n"
                "  SELECT DISTINCT ON (symbol) symbol, side, remaining_quantity, entry_price,\n"
                "    notional / NULLIF(price * fill_quantity, 0) AS multiplier\n"
                "  FROM position_events\n"
                "  WHERE run_id = '${run_id}' AND account_id IN (${account_id:sqlstring})\n"
                f"  {_latest_position_event_order()}\n"
                "),\n"
                "marks AS (\n"
                "  SELECT DISTINCT ON (o.symbol) o.symbol, o.close AS mark\n"
                "  FROM ohlcv o, meta m\n"
                "  WHERE o.timeframe = m.timeframe\n"
                f"    AND {_data_source_filter('m', 'o')}\n"
                "    AND o.ts <= CASE WHEN m.mode = 'backtest' THEN m.ended_at ELSE now() END\n"
                "  ORDER BY o.symbol, o.ts DESC\n"
                "),\n"
                "pnl AS (\n"
                "  SELECT SUM((CASE WHEN p.side='long' THEN mk.mark - p.entry_price\n"
                "    ELSE p.entry_price - mk.mark END) * p.remaining_quantity * p.multiplier) AS value\n"
                "  FROM positions p JOIN marks mk ON mk.symbol = p.symbol\n"
                "  WHERE p.remaining_quantity > 0\n"
                "),\n"
                "equity AS (\n"
                "  SELECT equity FROM equity_curve\n"
                "  WHERE run_id = '${run_id}' AND account_id IN (${account_id:sqlstring})\n"
                "  ORDER BY ts DESC LIMIT 1\n"
                ")\n"
                "SELECT COALESCE(\n"
                "  ROUND(pnl.value::numeric,0)::text || ' / ' ||\n"
                "    ROUND((pnl.value/NULLIF(e.equity,0)*100)::numeric,1)::text || '%',\n"
                "  'Flat'\n"
                ') AS "Unrealized P&L"\n'
                "FROM pnl CROSS JOIN equity e"
            )
        ],
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
                # "Flat" (no open positions) must win over the sign regexes
                # below — it has no leading "-" so ^[^-] would otherwise
                # color it green, falsely implying a profit.
                "mappings": [
                    {"type": "value", "options": {"Flat": {"color": "text"}}},
                    *_sign_color_mappings(),
                ],
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/"},
            "colorMode": "value",
            "graphMode": "none",
            "justifyMode": "center",
        },
    },
    {
        "_type": "kpi",
        "title": "Margin Utilization",
        "description": (
            'Margin locked across open positions, shown as "$ / % of equity" — '
            "same quantity, two formats, not two different numbers (unlike "
            "Portfolio Exposure's notional %, this is capital actually committed, "
            "see that panel's description). Equals gross exposure for spot-only "
            "runs (margin_rate=1.0). Breakdown by financing regime (who set the "
            "rate — unlevered/fixed/dynamic) is in Margin Locked by Mode next to "
            "Portfolio Exposure; per-position detail is in Position Snapshot's "
            "Margin Mode column."
        ),
        "type": "stat",
        "h": 4,
        "w": 4,
        "targets": [
            _stat_target(
                "WITH equity AS (\n"
                "  SELECT equity FROM equity_curve\n"
                "  WHERE run_id = '${run_id}' AND account_id IN (${account_id:sqlstring})\n"
                "  ORDER BY ts DESC LIMIT 1\n"
                "),\n"
                "positions AS (\n"
                "  SELECT DISTINCT ON (symbol) symbol, remaining_quantity, margin_locked\n"
                "  FROM position_events\n"
                "  WHERE run_id = '${run_id}' AND account_id IN (${account_id:sqlstring})\n"
                f"  {_latest_position_event_order()}\n"
                "),\n"
                "locked AS (\n"
                "  SELECT COALESCE(SUM(p.margin_locked), 0) AS margin_locked\n"
                "  FROM positions p WHERE p.remaining_quantity > 0\n"
                ")\n"
                "SELECT ROUND(l.margin_locked::numeric,0)::text || ' / ' ||\n"
                "  ROUND((l.margin_locked/NULLIF(e.equity,0)*100)::numeric,1)::text || '%'\n"
                '  AS "Margin Utilization"\n'
                "FROM locked l CROSS JOIN equity e"
            )
        ],
        "fieldConfig": {
            "defaults": {
                "color": {"fixedColor": "blue", "mode": "fixed"},
            },
            "overrides": [],
        },
        "options": {
            # Formatted text ("920 / 0.9%"), not a bare number — default
            # reduceOptions.fields (numeric-only) would show "No data" — see
            # Total Return's comment above for the same gotcha.
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/"},
            "colorMode": "value",
            "graphMode": "none",
            "justifyMode": "center",
        },
    },
    {"_type": "break"},
    # Row 2: how has it done overall — the whole-run summary, secondary to
    # "what's happening right now" in a monitoring-first dashboard.
    *[_KPI_CATALOGUE[k] for k in DEFAULT_KPIS],
    {"_type": "row", "title": "Performance Detail"},
    {
        "_type": "fixed",
        "_x": 12,
        "_dy": 0,
        "title": "Portfolio Equity Curve",
        "description": "Portfolio equity over time.",
        "type": "timeseries",
        "h": 5,
        "w": 12,
        "targets": [
            _target(
                'SELECT ts AS time, equity AS "Strategy"'
                " FROM equity_curve WHERE run_id = '${run_id}'"
                " AND account_id IN (${account_id:sqlstring}) AND $__timeFilter(ts) ORDER BY ts"
            )
        ],
        "fieldConfig": {
            "defaults": {
                "custom": {
                    "lineWidth": 2,
                    "fillOpacity": 10,
                    "gradientMode": "scheme",
                    "showPoints": "never",
                }
            },
            "overrides": [
                _color_override("Strategy", "green"),
            ],
        },
        "options": {
            "tooltip": {"mode": "multi"},
            "legend": {"displayMode": "list", "placement": "bottom"},
        },
    },
    {
        "_type": "fixed",
        "_x": 12,
        "_dy": 10,
        "title": "Trade Return Distribution",
        "description": (
            "Realized return % per closed trade (reduce/close only), binned. Return\n"
            "normalizes across position sizes, unlike raw P&L — trade-count based,\n"
            "not time-based, so it stays readable for high-frequency strategies."
        ),
        "type": "histogram",
        "h": 5,
        "w": 6,
        "targets": [
            _target(
                'SELECT ROUND(net_return::numeric,4)::float8 AS "Return"'
                " FROM position_events WHERE run_id = '${run_id}'"
                " AND account_id IN (${account_id:sqlstring}) AND net_return IS NOT NULL"
                " AND $__timeFilter(ts)",
                "A",
                "table",
            )
        ],
        "fieldConfig": {
            "defaults": {
                "unit": "percent",
                "custom": {"fillOpacity": 80, "lineWidth": 0},
                "color": {"fixedColor": "blue", "mode": "fixed"},
            },
            "overrides": [],
        },
        "options": {
            "legend": {"showLegend": False},
        },
    },
    {
        "_type": "fixed",
        "_x": 0,
        "_dy": 0,
        "title": "Price Trend — ${symbol}",
        "description": "Close price for the selected symbol, matched by timeframe and data source.",
        "type": "timeseries",
        "h": 10,
        "w": 12,
        "targets": [
            _target(
                "WITH meta AS ("
                " SELECT timeframe, data_source, started_at, ended_at"
                " FROM backtest_runs WHERE run_id = '${run_id}')"
                ' SELECT o.ts AS time, o.close AS "${symbol}"'
                " FROM ohlcv o, meta m"
                " WHERE o.symbol = '${symbol}'"
                " AND o.timeframe = m.timeframe"
                f" AND {_data_source_filter('m', 'o')}"
                " AND (m.started_at IS NULL OR o.ts >= m.started_at)"
                " AND (m.ended_at IS NULL OR o.ts <= m.ended_at)"
                " AND $__timeFilter(o.ts)"
                " ORDER BY o.ts",
                "A",
            ),
        ],
        "fieldConfig": {
            "defaults": {
                "custom": {"lineWidth": 1, "showPoints": "never"},
                "color": {"fixedColor": "#5794F2", "mode": "fixed"},
            },
            "overrides": [],
        },
        "options": {
            "tooltip": {"mode": "single"},
            "legend": {"displayMode": "list", "placement": "bottom"},
        },
    },
    # -- Trade Events (single table replacing old Trade Detail) --
    {
        "_type": "fixed",
        "_x": 0,
        "_dy": 15,
        "title": "Trade Events",
        "description": (
            "One row per fill event, all symbols in the run — filter via the Symbol\n"
            "column's icon. Lifecycle: open/add = entry, reduce/close = exit;\n"
            "P&L/Notional Return/Margin Return only populate on reduce/close rows.\n"
            "\n"
            "- `#` — row index.\n"
            "- Time — event timestamp.\n"
            "- Symbol — trading pair.\n"
            "- Event — open / add / reduce / close.\n"
            "- Side — long / short.\n"
            "- Quantity — this event's fill size (base-asset units).\n"
            "- Trade Price — this event's fill price.\n"
            "- Cash Flow — net cash impact of this event, account currency:\n"
            "  negative on open/add (capital deployed), positive on reduce/close\n"
            "  (capital + PnL returned). Already nets out Cost — not another copy\n"
            "  of it.\n"
            "- P&L — realized profit/loss on this event, account currency\n"
            "  (reduce/close only).\n"
            "- Notional Return — price return, % of notional (reduce/close only).\n"
            "- Margin Return — % return on the capital locked for the closed qty;\n"
            "  = Notional Return for spot, amplified by leverage for futures.\n"
            "- Margin Mode — who sets this side's margin rate: unlevered (spot/\n"
            "  cash), fixed (exchange/regulator-set, e.g. TAIFEX margin or Reg-T),\n"
            "  dynamic (trader-chosen leverage, e.g. isolated-margin perps). Same\n"
            "  Leverage number means different things under each mode.\n"
            "- Entry Price — weighted-average cost basis after this event, not\n"
            "  this row's own Trade Price.\n"
            "- Position — running position size after this event, not this\n"
            "  event's Quantity.\n"
            "- Cost — commission + slippage + tax for this event, account currency.\n"
            "- Group — identifies related legs (e.g. a spot+perp arb pair).\n"
            "  Backtest/sim stages them all-or-none locally. Live records serial\n"
            "  broker fills; the label does not imply venue atomicity.\n"
            "- Trade ID — symbol + this trade's open time; identifies one\n"
            "  open→close round-trip.\n"
            "- Periods — elapsed bars, not clock time — multiply by the run's\n"
            "  timeframe for actual duration.\n"
            "- Reason — free text, or one of 5 risk-exit codes: stop_loss,\n"
            "  take_profit, liquidation, drawdown_breach, force_close."
        ),
        "type": "table",
        "h": 15,
        "w": 24,
        "targets": [
            _target(
                "SELECT"
                ' ROW_NUMBER() OVER (ORDER BY ts) AS "#",'
                ' ts AS "Time",'
                ' symbol AS "Symbol",'
                ' event_type AS "Event",'
                ' side AS "Side",'
                ' ROUND(fill_quantity::numeric,4) AS "Quantity",'
                ' ROUND(price::numeric,2) AS "Trade Price",'
                ' ROUND(cash_flow::numeric,2) AS "Cash Flow",'
                ' ROUND(pnl::numeric,2) AS "P&L",'
                ' ROUND(net_return::numeric,2) AS "Notional Return",'
                ' ROUND(margin_roi::numeric,2) AS "Margin Return",'
                ' margin_mode AS "Margin Mode",'
                ' ROUND(entry_price::numeric,2) AS "Entry Price",'
                ' ROUND(remaining_quantity::numeric,4) AS "Position",'
                ' ROUND((commission + slippage + tax)::numeric,2) AS "Cost",'
                ' group_id AS "Group",'
                " symbol || ' @ ' || to_char(entry_at, 'YYYY-MM-DD HH24:MI:SS')"
                ' AS "Trade ID",'
                ' periods_held AS "Periods",'
                ' reason AS "Reason"'
                " FROM position_events WHERE run_id = '${run_id}'"
                " AND account_id IN (${account_id:sqlstring})"
                " AND $__timeFilter(ts)"
                " ORDER BY ts",
                "A",
                "table",
            )
        ],
        "fieldConfig": {
            "defaults": {"custom": {"filterable": True}},
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "Notional Return"},
                    "properties": [
                        {"id": "unit", "value": "percent"},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "Margin Return"},
                    "properties": [
                        {"id": "unit", "value": "percent"},
                    ],
                },
                # Widths sized to actual content (timestamps/symbols need room,
                # short enums/numbers don't) instead of Grafana's equal-split
                # default. Reason is left unset — free-text, takes the remainder.
                _width_override("#", 40),
                _width_override("Time", 180),
                _width_override("Symbol", 140),
                _width_override("Event", 90),
                _width_override("Side", 70),
                _width_override("Quantity", 100),
                _width_override("Trade Price", 110),
                _width_override("Cash Flow", 110),
                _width_override("P&L", 100),
                _width_override("Notional Return", 120),
                _width_override("Margin Return", 120),
                _width_override("Margin Mode", 100),
                _width_override("Entry Price", 110),
                _width_override("Position", 100),
                _width_override("Cost", 80),
                _width_override("Group", 140),
                _width_override("Trade ID", 260),  # "symbol @ entry_at" — both concatenated
                _width_override("Periods", 80),
            ],
        },
        "options": {
            "showHeader": True,
            "sortBy": [{"displayName": "Time", "desc": False}],
        },
    },
    {
        "_type": "fixed",
        "_x": 0,
        "_dy": 10,
        "title": "Entry / Exit Signals — ${symbol}",
        "description": "Signal decision time (1 period before fill). Entry = open/add, Exit = reduce/close.",
        "type": "timeseries",
        "h": 5,
        "w": 12,
        "targets": [
            _target(
                "SELECT te.ts - CASE UPPER(br.timeframe)"
                " WHEN 'H1' THEN interval '1 hour' WHEN '1H' THEN interval '1 hour'"
                " WHEN 'M5' THEN interval '5 minutes' WHEN '5M' THEN interval '5 minutes'"
                " WHEN 'M15' THEN interval '15 minutes' WHEN '15M' THEN interval '15 minutes'"
                " WHEN 'H4' THEN interval '4 hours' WHEN '4H' THEN interval '4 hours'"
                " WHEN 'D1' THEN interval '1 day' WHEN '1D' THEN interval '1 day'"
                " ELSE interval '1 hour' END AS time,"
                ' te.price AS "Entry"'
                " FROM position_events te"
                " JOIN backtest_runs br ON br.run_id = te.run_id"
                " WHERE te.run_id = '${run_id}'"
                " AND te.account_id IN (${account_id:sqlstring})"
                " AND te.symbol = '${symbol}'"
                " AND te.event_type IN ('open', 'add')"
                " AND $__timeFilter(te.ts)",
                "A",
            ),
            _target(
                "SELECT te.ts - CASE UPPER(br.timeframe)"
                " WHEN 'H1' THEN interval '1 hour' WHEN '1H' THEN interval '1 hour'"
                " WHEN 'M5' THEN interval '5 minutes' WHEN '5M' THEN interval '5 minutes'"
                " WHEN 'M15' THEN interval '15 minutes' WHEN '15M' THEN interval '15 minutes'"
                " WHEN 'H4' THEN interval '4 hours' WHEN '4H' THEN interval '4 hours'"
                " WHEN 'D1' THEN interval '1 day' WHEN '1D' THEN interval '1 day'"
                " ELSE interval '1 hour' END AS time,"
                ' te.price AS "Exit"'
                " FROM position_events te"
                " JOIN backtest_runs br ON br.run_id = te.run_id"
                " WHERE te.run_id = '${run_id}'"
                " AND te.account_id IN (${account_id:sqlstring})"
                " AND te.symbol = '${symbol}'"
                " AND te.event_type IN ('reduce', 'close')"
                " AND $__timeFilter(te.ts)",
                "B",
            ),
        ],
        "fieldConfig": {
            "defaults": {"custom": {"lineWidth": 0, "showPoints": "always", "pointSize": 12}},
            "overrides": [
                _color_override("Entry", "green"),
                _color_override("Exit", "red"),
            ],
        },
        "options": {
            "tooltip": {"mode": "single"},
            "legend": {"displayMode": "list", "placement": "bottom"},
        },
    },
    {
        "_type": "fixed",
        "_x": 0,
        "_dy": 30,
        "title": "Position Snapshot",
        "description": (
            "Holdings as of the time range's end (top-right picker) — one row per\n"
            "symbol, sorted by |Weight| (biggest exposure first). Point-in-time\n"
            "reconstruction, not an explicit rebalance list — drag the picker to a\n"
            "past date to see positions as of that point instead.\n"
            "\n"
            "- `#` — rank by |Weight|, largest exposure first.\n"
            "- Time — this position's open time (entry_at).\n"
            "- Symbol — trading pair.\n"
            "- Side — long / short.\n"
            "- Weight — position notional as % of equity, signed by side\n"
            "  (negative = short).\n"
            "- Entry Price — weighted-average cost basis across all fills to date.\n"
            "- Position — current running size (base-asset units).\n"
            "- Market Price — latest close at/before the picker's end time.\n"
            "- MTM P&L — mark-to-market P&L at that time, account currency —\n"
            "  not 'unrealized' in the live sense, a past date's position may\n"
            "  since be closed.\n"
            "- MTM Return — same, as % of Entry Price.\n"
            "- Margin Locked — capital committed to this position, account\n"
            "  currency (= notional for spot).\n"
            "- Leverage — notional / Margin Locked (1.0 for spot).\n"
            "- Margin Mode — who sets this side's margin rate: unlevered (spot/\n"
            "  cash), fixed (exchange/regulator-set, e.g. TAIFEX margin or Reg-T),\n"
            "  dynamic (trader-chosen leverage, e.g. isolated-margin perps). Same\n"
            "  Leverage number means different things under each mode.\n"
            "- Liquidation Price — price at which this position liquidates;\n"
            "  null unless the market's maintenance_margin_rate is set.\n"
            "- Liquidation Buffer — how far Market Price sits from Liquidation\n"
            "  Price, % of Market Price; null under the same condition.\n"
            "- Trade ID — symbol + this position's open time; filter it in Trade\n"
            "  Events for the full fill history behind this row."
        ),
        "type": "table",
        "h": 8,
        "w": 24,
        "targets": [
            _target(
                "WITH meta AS ("
                " SELECT timeframe, data_source FROM backtest_runs WHERE run_id='${run_id}'"
                "),\n"
                "equity AS (\n"
                "  SELECT equity FROM equity_curve\n"
                "  WHERE run_id = '${run_id}' AND account_id IN (${account_id:sqlstring})\n"
                "    AND ts <= $__timeTo()\n"
                "  ORDER BY ts DESC LIMIT 1\n"
                "),\n"
                "positions AS (\n"
                "  SELECT DISTINCT ON (symbol) symbol, side, remaining_quantity,\n"
                "    entry_price, entry_at, margin_locked, leverage, liquidation_price,\n"
                "    margin_mode,\n"
                "    notional / NULLIF(price * fill_quantity, 0) AS multiplier\n"
                "  FROM position_events\n"
                "  WHERE run_id = '${run_id}' AND account_id IN (${account_id:sqlstring})\n"
                "    AND ts <= $__timeTo()\n"
                f"  {_latest_position_event_order()}\n"
                "),\n"
                "marks AS (\n"
                "  SELECT DISTINCT ON (o.symbol) o.symbol, o.close AS market_price\n"
                "  FROM ohlcv o, meta m\n"
                "  WHERE o.timeframe = m.timeframe\n"
                f"    AND {_data_source_filter('m', 'o')}\n"
                "    AND o.ts <= $__timeTo()\n"
                "  ORDER BY o.symbol, o.ts DESC\n"
                "),\n"
                "sized AS (\n"
                "  SELECT p.symbol, p.side, p.remaining_quantity, p.entry_price, p.entry_at,\n"
                "    p.margin_locked, p.leverage, p.liquidation_price, p.margin_mode,\n"
                "    mk.market_price, p.multiplier,\n"
                "    (CASE WHEN p.side='long' THEN 1 ELSE -1 END)\n"
                "      * p.remaining_quantity * mk.market_price * p.multiplier AS signed_notional\n"
                "  FROM positions p JOIN marks mk ON mk.symbol = p.symbol\n"
                "  WHERE p.remaining_quantity > 0\n"
                ")\n"
                "SELECT ROW_NUMBER() OVER"
                ' (ORDER BY ABS(s.signed_notional / NULLIF(e.equity,0)) DESC) AS "#",\n'
                '  s.entry_at AS "Time",\n'
                '  s.symbol AS "Symbol", s.side AS "Side",\n'
                '  ROUND((s.signed_notional / NULLIF(e.equity,0))::numeric,4) AS "Weight",\n'
                '  ROUND(s.entry_price::numeric,2) AS "Entry Price",\n'
                '  ROUND(s.remaining_quantity::numeric,4) AS "Position",\n'
                '  ROUND(s.market_price::numeric,2) AS "Market Price",\n'
                "  ROUND(((CASE WHEN s.side='long' THEN s.market_price - s.entry_price\n"
                "    ELSE s.entry_price - s.market_price END) * s.remaining_quantity * s.multiplier)"
                '    ::numeric,2) AS "MTM P&L",\n'
                "  ROUND(((CASE WHEN s.side='long' THEN s.market_price - s.entry_price\n"
                "    ELSE s.entry_price - s.market_price END) / NULLIF(s.entry_price,0))"
                '    ::numeric,4) AS "MTM Return",\n'
                '  ROUND(s.margin_locked::numeric,2) AS "Margin Locked",\n'
                '  ROUND(s.leverage::numeric,2) AS "Leverage",\n'
                '  s.margin_mode AS "Margin Mode",\n'
                '  ROUND(s.liquidation_price::numeric,2) AS "Liquidation Price",\n'
                "  ROUND((CASE WHEN s.liquidation_price IS NULL THEN NULL\n"
                "    WHEN s.side='long' THEN (s.market_price - s.liquidation_price) / NULLIF(s.market_price,0)\n"
                "    ELSE (s.liquidation_price - s.market_price) / NULLIF(s.market_price,0) END)"
                '    ::numeric,4) AS "Liquidation Buffer",\n'
                "  s.symbol || ' @ ' || to_char(s.entry_at, 'YYYY-MM-DD HH24:MI:SS')"
                ' AS "Trade ID"\n'
                "FROM sized s CROSS JOIN equity e\n"
                "ORDER BY ABS(s.signed_notional / NULLIF(e.equity,0)) DESC",
                "A",
                "table",
            )
        ],
        "fieldConfig": {
            "defaults": {},
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "Weight"},
                    "properties": [{"id": "unit", "value": "percentunit"}],
                },
                {
                    "matcher": {"id": "byName", "options": "MTM P&L"},
                    "properties": [{"id": "custom.footer", "value": {"reducers": ["sum"]}}],
                },
                {
                    "matcher": {"id": "byName", "options": "MTM Return"},
                    "properties": [{"id": "unit", "value": "percentunit"}],
                },
                {
                    "matcher": {"id": "byName", "options": "Leverage"},
                    "properties": [{"id": "unit", "value": "none"}, {"id": "decimals", "value": 2}],
                },
                {
                    "matcher": {"id": "byName", "options": "Liquidation Buffer"},
                    "properties": [{"id": "unit", "value": "percentunit"}],
                },
                _width_override("#", 40),
                _width_override("Time", 180),
                _width_override("Symbol", 90),
                _width_override("Side", 60),
                _width_override("Weight", 90),
                _width_override("Entry Price", 90),
                _width_override("Position", 80),
                _width_override("Market Price", 100),
                _width_override("MTM P&L", 90),
                _width_override("MTM Return", 90),
                _width_override("Margin Locked", 100),
                _width_override("Leverage", 80),
                _width_override("Margin Mode", 100),
                _width_override("Liquidation Price", 110),
                _width_override("Liquidation Buffer", 130),
                _width_override("Trade ID", 220),
            ],
        },
        "options": {
            # No sortBy: Grafana's column sort is signed-value only (no
            # sort-by-magnitude option), which would fight the query's
            # ORDER BY ABS(...) DESC — leave row order exactly as queried.
            "showHeader": True,
        },
    },
    {
        "_type": "fixed",
        "_x": 12,
        "_dy": 5,
        "title": "Portfolio Exposure",
        "description": "Gross/Net/Concentration as % of current equity — notional exposure, not margin usage. Concentration is whichever position is currently largest; it can shift between symbols, so check Position Snapshot to see which one.",
        "type": "timeseries",
        "h": 5,
        "w": 12,
        "targets": [
            _target(
                'SELECT ts AS time, gross_exposure AS "Gross", net_exposure AS "Net",'
                ' concentration AS "Concentration"'
                " FROM equity_curve WHERE run_id = '${run_id}'"
                " AND account_id IN (${account_id:sqlstring}) AND $__timeFilter(ts) ORDER BY ts"
            )
        ],
        "fieldConfig": {
            "defaults": {
                "unit": "percentunit",
                "custom": {"lineWidth": 1, "fillOpacity": 0, "showPoints": "never"},
            },
            "overrides": [
                _color_override("Gross", "orange"),
                _color_override("Net", "blue"),
                _color_override("Concentration", "red"),
            ],
        },
        "options": {
            "tooltip": {"mode": "multi"},
            "legend": {"displayMode": "list", "placement": "bottom"},
        },
    },
    {
        "_type": "fixed",
        "_x": 18,
        "_dy": 10,
        "title": "Margin Locked by Mode",
        "description": (
            "Current margin_locked across open positions, split by who sets the\n"
            "rate (unlevered/fixed/dynamic — see Position Snapshot's Margin Mode\n"
            "column), as % of equity. Same total as Margin Utilization, broken\n"
            "down by composition instead of blended into one number — a fixed\n"
            "(exchange-set) bar and a dynamic (self-chosen leverage) bar of equal\n"
            "size carry very different headroom-to-add-risk implications."
        ),
        "type": "bargauge",
        "h": 5,
        "w": 6,
        "targets": [
            _stat_target(
                "WITH equity AS (\n"
                "  SELECT equity FROM equity_curve\n"
                "  WHERE run_id = '${run_id}' AND account_id IN (${account_id:sqlstring})\n"
                "  ORDER BY ts DESC LIMIT 1\n"
                "),\n"
                "positions AS (\n"
                "  SELECT DISTINCT ON (symbol) symbol, remaining_quantity, margin_locked, margin_mode\n"
                "  FROM position_events\n"
                "  WHERE run_id = '${run_id}' AND account_id IN (${account_id:sqlstring})\n"
                f"  {_latest_position_event_order()}\n"
                ")\n"
                "SELECT\n"
                "  SUM(CASE WHEN p.margin_mode='unlevered' THEN p.margin_locked ELSE 0 END)\n"
                '    / NULLIF(MAX(e.equity),0) AS "Unlevered",\n'
                "  SUM(CASE WHEN p.margin_mode='fixed' THEN p.margin_locked ELSE 0 END)\n"
                '    / NULLIF(MAX(e.equity),0) AS "Fixed",\n'
                "  SUM(CASE WHEN p.margin_mode='dynamic' THEN p.margin_locked ELSE 0 END)\n"
                '    / NULLIF(MAX(e.equity),0) AS "Dynamic"\n'
                "FROM positions p CROSS JOIN equity e\n"
                "WHERE p.remaining_quantity > 0"
            )
        ],
        "fieldConfig": {
            "defaults": {
                "unit": "percentunit",
                "min": 0,
                "color": {"mode": "fixed"},
            },
            "overrides": [
                _color_override("Unlevered", _MARGIN_MODE_COLORS["unlevered"]),
                _color_override("Fixed", _MARGIN_MODE_COLORS["fixed"]),
                _color_override("Dynamic", _MARGIN_MODE_COLORS["dynamic"]),
            ],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/"},
            "orientation": "horizontal",
            "displayMode": "gradient",
        },
    },
    {
        "_type": "fixed",
        "_x": 0,
        "_dy": 38,
        "title": "Runtime Events",
        "description": (
            "Operational audit trail, not trade activity.\n"
            "\n"
            "- `#` — row index.\n"
            "- Time — event timestamp.\n"
            "- Symbol — trading pair; blank for run-level events (state_recovered).\n"
            "- Event — state_recovered (process resumed from checkpoint after a\n"
            "  restart, sim/live only) or decision_skipped (a risk/sizing\n"
            "  constraint blocked an order this period).\n"
            "- Reason — why a decision was skipped, e.g. insufficient_cash,\n"
            "  opposite_side, notional_capped; blank for state_recovered.\n"
            "- Detail — full raw event payload behind Reason, for deeper checks."
        ),
        "type": "table",
        "h": 8,
        "w": 24,
        "targets": [
            _target(
                "SELECT"
                ' ROW_NUMBER() OVER (ORDER BY ts) AS "#",'
                ' ts AS "Time",'
                ' symbol AS "Symbol",'
                ' event_type AS "Event",'
                " detail->>'reason' AS \"Reason\","
                ' detail::text AS "Detail"'
                " FROM runtime_events WHERE run_id = '${run_id}'"
                " AND $__timeFilter(ts)"
                " ORDER BY ts",
                "A",
                "table",
            )
        ],
        "fieldConfig": {
            "defaults": {"custom": {"filterable": True}},
            "overrides": [
                # Event is deliberately uncolored — both event types are
                # expected, by-design occurrences, not alarms.
                _width_override("#", 40),
                _width_override("Time", 180),
                _width_override("Symbol", 120),
                _width_override("Event", 140),
                _width_override("Reason", 140),
            ],
        },
        "options": {
            "showHeader": True,
            "sortBy": [{"displayName": "Time", "desc": False}],
        },
    },
]


def build_panels(panel_defs: list[dict]) -> list[dict]:
    """Assign id, gridPos, datasource to panel definitions in definition order."""
    panels: list[dict] = []
    panel_id = 1
    x = 0
    y = 0
    row_h = 0  # tallest panel in the current row

    fixed_defs: list[dict] = []
    valid_types = {"kpi", "half", "fixed", "row", "full_row", "break"}
    for defn in panel_defs:
        ptype = defn.get("_type")
        if ptype is None:
            raise ValueError(f"Missing _type in panel {defn.get('title', '?')!r}")
        if ptype not in valid_types:
            raise ValueError(f"Unknown panel _type: {ptype!r} in panel {defn.get('title', '?')!r}")
        if ptype == "fixed":
            fixed_defs.append(defn)
            continue

        # WHY: flush incomplete row before block-level panels (row / full_row / break)
        if ptype in ("row", "full_row", "break") and x > 0:
            y += row_h
            x = 0
            row_h = 0

        if ptype == "break":
            # Forces the next kpi/half panel onto a fresh row, leaving any
            # unfilled width in the current row blank — unlike "row", this
            # renders nothing (no title bar, no panel_id) so two adjacent
            # groups of tiles don't visually bleed into each other without
            # adding a divider between them.
            continue

        if ptype == "row":
            panels.append(_materialize_row(defn, panel_id, y))
            panel_id += 1
            y += 1
        elif ptype == "full_row":
            panels.append(_materialize_panel(defn, panel_id, 0, y))
            panel_id += 1
            y += defn["h"]
        elif ptype in ("kpi", "half"):
            panels.append(_materialize_panel(defn, panel_id, x, y))
            panel_id += 1
            row_h = max(row_h, defn["h"])
            x += defn["w"]
            if x >= 24:
                y += row_h
                x = 0
                row_h = 0

    # WHY: flush any trailing incomplete row before placing fixed panels
    if x > 0:
        y += row_h
    for defn in fixed_defs:
        panels.append(_materialize_panel(defn, panel_id, defn["_x"], y + defn["_dy"]))
        panel_id += 1

    return panels


def _materialize_panel(defn: dict, panel_id: int, x: int, y: int) -> dict:
    p = copy.deepcopy(defn)
    p.pop("_type", None)
    p.pop("_x", None)
    p.pop("_dy", None)
    h = p.pop("h")
    w = p.pop("w")
    p["id"] = panel_id
    p["gridPos"] = {"h": h, "w": w, "x": x, "y": y}
    p["datasource"] = DATASOURCE
    return p


def _materialize_row(defn: dict, panel_id: int, y: int) -> dict:
    """Build a Grafana collapsible row panel."""
    return {
        "id": panel_id,
        "type": "row",
        "title": defn["title"],
        "collapsed": False,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
        "panels": [],
    }


def _make_custom_variable(
    name: str,
    options: list[tuple[str, str]],
    *,
    label: str | None = None,
) -> dict:
    """Build a Grafana custom-type template variable."""
    csv = ",".join(f"{text} : {value}" for text, value in options)
    grafana_options = [
        {"text": text, "value": value, "selected": i == 0}
        for i, (text, value) in enumerate(options)
    ]
    v: dict = {
        "name": name,
        "type": "custom",
        "query": csv,
        "current": {"text": options[0][0], "value": options[0][1]},
        "options": grafana_options,
        "hide": 0,
        "includeAll": False,
        "multi": False,
    }
    if label:
        v["label"] = label
    return v


def _make_query_variable(
    name: str,
    sql: str,
    *,
    hide: int = 0,
    label: str | None = None,
    multi: bool = False,
) -> dict:
    """Build a Grafana query-type template variable.

    ``multi=True`` also enables "All" (default expands to every option, so
    ``${name:sqlstring}`` interpolates a valid SQL ``IN (...)`` list whether
    one, several, or all values are selected).
    """
    v: dict = {
        "name": name,
        "type": "query",
        "datasource": DATASOURCE,
        "definition": sql,
        "query": sql,
        "rawQuery": True,
        "refresh": 2,
        "regex": "",
        "includeAll": multi,
        "sort": 0,
        "current": {"text": "All", "value": ["$__all"]} if multi else {},
        "hide": hide,
        "multi": multi,
    }
    if label:
        v["label"] = label
    return v


def render_unified_dashboard() -> dict:
    """Build the single unified Strategy Dashboard."""
    all_defs = list(BASE_PANELS_DEF)
    panels = build_panels(all_defs)

    mode_var = _make_custom_variable(
        "mode",
        [("Backtest", "backtest"), ("Sim", "sim"), ("Live", "live")],
        label="Mode",
    )
    strategy_var = _make_query_variable(
        "strategy_name",
        "SELECT DISTINCT strategy_name FROM backtest_runs WHERE mode='${mode}' ORDER BY strategy_name",
        label="Strategy",
    )
    run_id_var = _make_query_variable(
        "run_id",
        "SELECT run_id FROM backtest_runs"
        " WHERE mode='${mode}' AND strategy_name='${strategy_name}'"
        " ORDER BY run_at DESC LIMIT 20",
        label="Run ID",
    )
    # multi + includeAll: defaults to "All", which re-resolves to whatever
    # accounts are actually valid for the selected run_id — a single stale
    # account selection can't survive a run_id switch and go silently
    # unmatched (Grafana doesn't reset an invalid current value on its own
    # when a parent variable changes; this sidesteps that instead of relying
    # on it). Still narrowable to one account manually when comparing.
    account_id_var = _make_query_variable(
        "account_id",
        "SELECT DISTINCT account_id FROM equity_curve WHERE run_id='${run_id}' ORDER BY account_id",
        label="Account",
        multi=True,
    )
    # Read-only context, not a filter: every symbol in a run is validated at
    # resolve_symbol() to share the account's currency (librae has no FX —
    # see RunConfig.account's docstring), so this is always exactly one
    # value. Shown once here instead of repeating it on every $ panel/row.
    currency_var = _make_query_variable(
        "currency",
        "SELECT currency FROM strategy_performance"
        " WHERE run_id='${run_id}' AND account_id IN (${account_id:sqlstring}) LIMIT 1",
        label="Currency",
    )
    symbol_var = _make_query_variable(
        "symbol",
        "SELECT jsonb_array_elements_text(symbols) AS symbol"
        " FROM backtest_runs WHERE run_id='${run_id}' ORDER BY symbol",
        label="Symbol",
    )

    return {
        "uid": "strategy-dashboard",
        "title": "Strategy",
        "description": "Unified strategy dashboard — generated by generate_dashboards.py",
        "tags": [],
        "timezone": "browser",
        "editable": True,
        "time": {"from": "now-1y", "to": "now"},
        "refresh": "5m",
        "templating": {
            "list": [mode_var, strategy_var, run_id_var, account_id_var, currency_var, symbol_var],
        },
        "graphTooltip": 1,
        "annotations": {"list": []},
        "panels": panels,
        "schemaVersion": 39,
        "version": 1,
    }


# ======================================================================
# Account Overview Dashboard
# ======================================================================

_ACCOUNT_OVERVIEW_SQL = (
    """WITH latest_equity AS (
  SELECT DISTINCT ON (ec.run_id, ec.account_id)
    ec.run_id, ec.account_id, ec.currency, ec.ts, ec.equity,
    ec.gross_exposure, ec.net_exposure, ec.concentration
  FROM equity_curve ec
  JOIN backtest_runs br ON br.run_id = ec.run_id
  WHERE ec.account_id IN (${account_id:sqlstring})
    AND ec.currency IN (${currency:sqlstring})
    AND br.mode IN (${mode:sqlstring})
    AND $__timeFilter(ec.ts)
  ORDER BY ec.run_id, ec.account_id, ec.ts DESC
),
position_counts AS (
  SELECT le.run_id, le.account_id,
    COUNT(*) FILTER (WHERE p.remaining_quantity > 0) AS open_positions
  FROM latest_equity le
  LEFT JOIN LATERAL (
    SELECT DISTINCT ON (pe.symbol) pe.remaining_quantity
    FROM position_events pe
    WHERE pe.run_id = le.run_id
      AND pe.account_id = le.account_id
      AND pe.currency = le.currency
      AND pe.ts <= le.ts
"""
    f"    {_latest_position_event_order('pe')}"
    """
  ) p ON true
  GROUP BY le.run_id, le.account_id
)
SELECT
  br.strategy_name AS "Strategy",
  br.mode AS "Mode",
  le.run_id AS "Run ID",
  le.ts AS "Last Equity",
"""
    f'  {_runtime_status_case("br")} AS "Status",'
    """
  br.last_heartbeat_at AS "Heartbeat",
  ROUND(le.equity::numeric, 2)::float8 AS "Equity",
  le.currency AS "Currency",
  ROUND(le.gross_exposure::numeric, 4)::float8 AS "Gross Exposure",
  ROUND(le.net_exposure::numeric, 4)::float8 AS "Net Exposure",
  ROUND(le.concentration::numeric, 4)::float8 AS "Concentration",
  pc.open_positions AS "Open Positions"
FROM latest_equity le
JOIN backtest_runs br ON br.run_id = le.run_id
JOIN position_counts pc
  ON pc.run_id = le.run_id AND pc.account_id = le.account_id
ORDER BY le.ts DESC, br.strategy_name, le.run_id"""
)


def render_account_overview_dashboard() -> dict:
    """Build the same-currency, per-run account overview dashboard."""
    table = {
        "_type": "full_row",
        "title": "Run Overview",
        "description": (
            "One latest-state row per run in the selected account, currency, modes, "
            "and time range. Exposure values are per-run equity fractions. Financial "
            "values are deliberately not summed across runs because portfolio-level "
            "aggregation and reporting-currency conversion are caller-owned."
        ),
        "type": "table",
        "h": 14,
        "w": 24,
        "targets": [_target(_ACCOUNT_OVERVIEW_SQL, fmt="table")],
        "fieldConfig": {
            "defaults": {"custom": {"filterable": True}},
            "overrides": [
                _width_override("Strategy", 180),
                _width_override("Mode", 90),
                _width_override("Run ID", 260),
                _width_override("Last Equity", 180),
                {
                    "matcher": {"id": "byName", "options": "Status"},
                    "properties": [
                        {"id": "mappings", "value": _status_value_mappings()},
                        {"id": "custom.width", "value": 90},
                    ],
                },
                _width_override("Heartbeat", 180),
                _width_override("Currency", 90),
                {
                    "matcher": {"id": "byName", "options": "Equity"},
                    "properties": [{"id": "decimals", "value": 2}],
                },
                {
                    "matcher": {
                        "id": "byRegexp",
                        "options": "/^(Gross Exposure|Net Exposure|Concentration)$/",
                    },
                    "properties": [
                        {"id": "unit", "value": "percentunit"},
                        {"id": "decimals", "value": 2},
                    ],
                },
            ],
        },
        "options": {
            "showHeader": True,
            "sortBy": [{"displayName": "Last Equity", "desc": True}],
        },
    }

    currency_var = _make_query_variable(
        "currency",
        "SELECT DISTINCT currency FROM equity_curve ORDER BY currency",
        label="Currency",
    )
    account_id_var = _make_query_variable(
        "account_id",
        "SELECT DISTINCT account_id FROM equity_curve"
        " WHERE currency IN (${currency:sqlstring}) ORDER BY account_id",
        label="Account",
    )
    mode_var = _make_query_variable(
        "mode",
        "SELECT DISTINCT br.mode FROM backtest_runs br"
        " JOIN equity_curve ec ON ec.run_id=br.run_id"
        " WHERE ec.currency IN (${currency:sqlstring})"
        " AND ec.account_id IN (${account_id:sqlstring})"
        " ORDER BY br.mode",
        label="Mode",
        multi=True,
    )

    return {
        "uid": "account-overview-dashboard",
        "title": "Account Overview",
        "description": (
            "Same-currency per-run account overview — generated by generate_dashboards.py"
        ),
        "tags": [],
        "timezone": "browser",
        "editable": True,
        "time": {"from": "now-24h", "to": "now"},
        "refresh": "1m",
        "templating": {"list": [currency_var, account_id_var, mode_var]},
        "graphTooltip": 1,
        "annotations": {"list": []},
        "panels": build_panels([table]),
        "schemaVersion": 39,
        "version": 1,
    }


# ======================================================================
# Signal Monitor Dashboard
# ======================================================================

# WHY: common SQL fragments for signal_events LATERAL JOIN to ohlcv.
# These are reused across multiple panels to compute forward return, MFE, MAE.
# All filtering uses run_id — symbol/timeframe/source derived from backtest_runs.
# _META_INNER is a single lookup that all CTEs inject as their first WITH clause,
# so symbol/timeframe/data_source are resolved once instead of once per column.
_SIG_WHERE = "s.run_id = '${run_id}' AND s.signal_type = '${signal_type}'"
_META_INNER = " SELECT timeframe, data_source FROM backtest_runs WHERE run_id='${run_id}'"
_OHLCV_WHERE = (
    "ohlcv.symbol = s.symbol"
    " AND ohlcv.timeframe = meta.timeframe"
    f" AND {_data_source_filter('meta', 'ohlcv')}"
)
_ENTRY_BAR = (
    f"SELECT $fill_price_field AS entry_price FROM ohlcv, meta"
    f" WHERE {_OHLCV_WHERE} AND ts > s.ts"
    f" ORDER BY ts LIMIT 1"
)
_EXIT_BAR = (
    f"SELECT close FROM ohlcv, meta"
    f" WHERE {_OHLCV_WHERE} AND ts > s.ts"
    f" ORDER BY ts LIMIT 1 OFFSET $n"
)
_FWD_CTE = (
    f"WITH meta AS ({_META_INNER}),\n"
    f"fwd AS (\n"
    f"  SELECT s.ts,\n"
    f"    $expected_direction * (exit_bar.close - entry_bar.entry_price)"
    f" / NULLIF(entry_bar.entry_price, 0) AS ret\n"
    f"  FROM signal_events s, meta\n"
    f"  JOIN LATERAL ({_ENTRY_BAR}) entry_bar ON true\n"
    f"  JOIN LATERAL ({_EXIT_BAR}) exit_bar ON true\n"
    f"  WHERE {_SIG_WHERE}\n"
    f"    AND $__timeFilter(s.ts)\n"
    f")\n"
)
_EXC_CTE = (
    f"WITH meta AS ({_META_INNER}),\n"
    f"exc AS (\n"
    f"  SELECT s.ts, exc.mfe, exc.mae,\n"
    f"    ROW_NUMBER() OVER (ORDER BY s.ts) AS rn\n"
    f"  FROM signal_events s, meta\n"
    f"  JOIN LATERAL (\n"
    f"    SELECT $fill_price_field AS entry_price, ts AS entry_at FROM ohlcv\n"
    f"    WHERE {_OHLCV_WHERE} AND ts > s.ts\n"
    f"    ORDER BY ts LIMIT 1\n"
    f"  ) entry_bar ON true\n"
    f"  JOIN LATERAL (\n"
    f"    SELECT\n"
    f"      MAX(GREATEST(0.0, CASE WHEN $expected_direction = 1"
    f" THEN (b.high - entry_bar.entry_price)"
    f" ELSE (entry_bar.entry_price - b.low)"
    f" END / NULLIF(entry_bar.entry_price, 0))) AS mfe,\n"
    f"      MAX(GREATEST(0.0, CASE WHEN $expected_direction = 1"
    f" THEN (entry_bar.entry_price - b.low)"
    f" ELSE (b.high - entry_bar.entry_price)"
    f" END / NULLIF(entry_bar.entry_price, 0))) AS mae\n"
    f"    FROM (\n"
    f"      SELECT high, low FROM ohlcv\n"
    f"      WHERE {_OHLCV_WHERE} AND ts > entry_bar.entry_at\n"
    f"      ORDER BY ts LIMIT $n\n"
    f"    ) b\n"
    f"  ) exc ON true\n"
    f"  WHERE {_SIG_WHERE}\n"
    f"    AND $__timeFilter(s.ts)\n"
    f")\n"
)

_TH_RED_YELLOW_GREEN = [
    {"color": "red", "value": None},
    {"color": "red", "value": -0.005},
    {"color": "yellow", "value": 0},
    {"color": "green", "value": 0.001},
]
_TH_EDGE = [
    {"color": "red", "value": None},
    {"color": "red", "value": 1},
    {"color": "yellow", "value": 1.5},
    {"color": "green", "value": 2},
]

SIGNAL_MONITOR_PANELS: list[dict] = [
    # --- Snapshot row ---
    {"_type": "row", "title": "Snapshot"},
    _stat_panel(
        "Unrealized PnL",
        (
            f"WITH meta AS ({_META_INNER}),\n"
            "latest_signal AS (\n"
            "  SELECT ts, signal_value,\n"
            f"    (SELECT close FROM ohlcv, meta WHERE {_OHLCV_WHERE}"
            " ORDER BY ts DESC LIMIT 1) AS current_close,\n"
            f"    (SELECT $fill_price_field FROM ohlcv, meta WHERE {_OHLCV_WHERE}"
            " AND ts > s.ts ORDER BY ts LIMIT 1) AS signal_price\n"
            f"  FROM signal_events s\n  WHERE {_SIG_WHERE}\n"
            "  ORDER BY ts DESC LIMIT 1\n)\n"
            "SELECT $expected_direction * (current_close - signal_price)"
            ' / NULLIF(signal_price, 0) AS "PnL"\n'
            "FROM latest_signal\n"
            "WHERE current_close IS NOT NULL AND signal_price IS NOT NULL"
        ),
        "percentunit",
        [
            {"color": "red", "value": None},
            {"color": "red", "value": -0.01},
            {"color": "yellow", "value": 0},
            {"color": "green", "value": 0.01},
        ],
        w=4,
        decimals=3,
        no_value="N/A",
        description="Latest signal's gross hypothetical return: expected_direction x (current_close - reference_price) / reference_price. Reference = next observed $fill_price_field; no costs or execution constraints.",
    ),
    _stat_panel(
        "Mean Fwd Return (T+$n)",
        _FWD_CTE + 'SELECT AVG(ret) AS "Mean Ret" FROM fwd',
        "percentunit",
        _TH_RED_YELLOW_GREEN,
        w=4,
        decimals=3,
        description="Gross direction-adjusted mean return. Reference = next observed $fill_price_field; T+1 is the following observed bar. No costs or execution constraints.",
    ),
    _stat_panel(
        "Edge Ratio (T+$n)",
        _EXC_CTE + 'SELECT AVG(mfe) / NULLIF(AVG(mae), 0) AS "Edge" FROM exc',
        None,
        _TH_EDGE,
        w=4,
        decimals=2,
        description="AVG(non-negative MFE) / AVG(non-negative MAE) over n observed bars. >2 = healthy, <1 = adverse exceeds favorable.",
    ),
    _stat_panel(
        "Last Signal Age",
        f'SELECT EXTRACT(EPOCH FROM NOW() - MAX(ts)) / 3600.0 AS "Age"'
        f" FROM signal_events s WHERE {_SIG_WHERE}",
        "h",
        [
            {"color": "green", "value": None},
            {"color": "yellow", "value": 24},
            {"color": "red", "value": 48},
        ],
        w=3,
        decimals=1,
        description="Hours since last signal. >48hr may indicate system failure.",
    ),
    _stat_panel(
        "N (Signals)",
        f'SELECT COUNT(*) AS "N" FROM signal_events s WHERE {_SIG_WHERE} AND $__timeFilter(ts)',
        None,
        [],
        w=3,
        fixed_color="blue",
        description="Total signal count in selected time range.",
    ),
    _stat_panel(
        "Signal Value",
        f'SELECT signal_value AS "Value" FROM signal_events s'
        f" WHERE {_SIG_WHERE} ORDER BY ts DESC LIMIT 1",
        None,
        [],
        w=3,
        decimals=3,
        no_value="N/A",
        fixed_color="blue",
        description="Latest signal_value.",
    ),
    _poll_seconds_panel(w=3),
    # --- Trend row ---
    {"_type": "row", "title": "Trend"},
    {
        "_type": "half",
        "title": "Price & Signals",
        "description": "Price (left axis) with signal firing points (right axis, orange dots).",
        "type": "timeseries",
        "h": 8,
        "w": 12,
        "targets": [
            _target(
                f"WITH meta AS ({_META_INNER})"
                f' SELECT ts AS time, close AS "Close" FROM ohlcv, meta'
                f" WHERE {_OHLCV_WHERE}"
                f" AND $__timeFilter(ts) ORDER BY ts",
                "price",
            ),
            _target(
                'SELECT ts AS time, signal_value AS "Signal"'
                f" FROM signal_events s WHERE {_SIG_WHERE}"
                " AND $__timeFilter(ts) ORDER BY ts",
                "signals",
            ),
        ],
        "fieldConfig": {
            "defaults": {"custom": {"lineWidth": 1, "fillOpacity": 0, "axisPlacement": "left"}},
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "Signal"},
                    "properties": [
                        {"id": "custom.axisPlacement", "value": "right"},
                        {"id": "custom.drawStyle", "value": "points"},
                        {"id": "custom.pointSize", "value": 8},
                        {"id": "color", "value": {"fixedColor": "orange", "mode": "fixed"}},
                        {"id": "custom.lineWidth", "value": 0},
                    ],
                }
            ],
        },
    },
    {
        "_type": "half",
        "title": "Cumulative Signal Return (T+$n)",
        "description": "Arithmetic sum of gross per-signal forward returns (not compounded). Reference = next observed $fill_price_field; outcome = n observed bars after the reference.",
        "type": "timeseries",
        "h": 8,
        "w": 12,
        "targets": [
            _target(
                _FWD_CTE + "SELECT ts AS time,\n"
                '  SUM(ret) OVER (ORDER BY ts) AS "Cumulative Return"\n'
                "FROM fwd ORDER BY ts"
            )
        ],
        "fieldConfig": {
            "defaults": {"unit": "percentunit", "custom": {"lineWidth": 2, "fillOpacity": 10}},
        },
    },
    {
        "_type": "half",
        "title": "Rolling $k Mean Return (T+$n)",
        "description": "Rolling k-signal average of gross direction-adjusted return. Reference = next observed $fill_price_field; outcome = n observed bars later.",
        "type": "timeseries",
        "h": 8,
        "w": 12,
        "targets": [
            _target(
                f"WITH meta AS ({_META_INNER}),\n"
                "fwd AS (\n"
                "  SELECT s.ts,\n"
                "    $expected_direction * (exit_bar.close - entry_bar.entry_price)"
                " / NULLIF(entry_bar.entry_price, 0) AS adj_return,\n"
                "    ROW_NUMBER() OVER (ORDER BY s.ts) AS rn\n"
                "  FROM signal_events s, meta\n"
                f"  JOIN LATERAL ({_ENTRY_BAR}) entry_bar ON true\n"
                f"  JOIN LATERAL ({_EXIT_BAR}) exit_bar ON true\n"
                f"  WHERE {_SIG_WHERE} AND $__timeFilter(s.ts)\n"
                ")\n"
                "SELECT ts AS time,\n"
                "  AVG(adj_return) OVER (ORDER BY ts ROWS BETWEEN ($k - 1) PRECEDING AND CURRENT ROW)"
                ' AS "Mean Return"\n'
                "FROM fwd WHERE rn >= $k ORDER BY ts"
            )
        ],
        "fieldConfig": {
            "defaults": {
                "unit": "percentunit",
                "custom": {"lineWidth": 2, "fillOpacity": 10},
                "thresholds": {"mode": "absolute", "steps": _TH_RED_YELLOW_GREEN},
            },
        },
    },
    {
        "_type": "half",
        "title": "Rolling $k Edge Ratio (T+$n)",
        "description": "Rolling k-signal AVG(MFE)/AVG(MAE) over observed bars after the reference bar. >2 = healthy, <1 = adverse exceeds favorable.",
        "type": "timeseries",
        "h": 8,
        "w": 12,
        "targets": [
            _target(
                _EXC_CTE + "SELECT ts AS time,\n"
                "  AVG(mfe) OVER (ORDER BY ts ROWS BETWEEN ($k - 1) PRECEDING AND CURRENT ROW)\n"
                "  / NULLIF(AVG(mae) OVER (ORDER BY ts ROWS BETWEEN ($k - 1) PRECEDING AND CURRENT ROW), 0)\n"
                '  AS "Edge Ratio"\n'
                "FROM exc WHERE rn >= $k ORDER BY ts"
            )
        ],
        "fieldConfig": {
            "defaults": {
                "custom": {"lineWidth": 2, "fillOpacity": 10},
                "thresholds": {"mode": "absolute", "steps": _TH_EDGE},
            },
        },
    },
]


def _make_textbox_variable(name: str, default: str, *, label: str | None = None) -> dict:
    v: dict = {"name": name, "type": "textbox", "query": default}
    if label:
        v["label"] = label
    return v


def render_signal_monitor() -> dict:
    """Build the Signal Monitor dashboard."""
    panels = build_panels(SIGNAL_MONITOR_PANELS)

    variables = [
        _make_custom_variable(
            "mode",
            [("Backtest", "backtest"), ("Sim", "sim")],
            label="Mode",
        ),
        _make_query_variable(
            "strategy_name",
            "SELECT DISTINCT strategy_name FROM backtest_runs WHERE mode='${mode}' ORDER BY strategy_name",
            label="Strategy",
        ),
        _make_query_variable(
            "run_id",
            "SELECT run_id FROM backtest_runs"
            " WHERE mode='${mode}' AND strategy_name='${strategy_name}'"
            " ORDER BY run_at DESC LIMIT 20",
            label="Run ID",
        ),
        _make_textbox_variable("n", "24", label="Forward Horizon (bars)"),
        _make_textbox_variable("k", "50", label="Rolling Window (signals)"),
        _make_custom_variable(
            "fill_price_field",
            [("Close", "close"), ("Open", "open"), ("High", "high"), ("Low", "low")],
            label="Reference Price Field",
        ),
        _make_custom_variable(
            "signal_type",
            [("Entry", "entry"), ("Exit", "exit")],
            label="Signal Event",
        ),
        _make_custom_variable(
            "expected_direction",
            [("Long", "1"), ("Short", "-1")],
            label="Expected Direction",
        ),
    ]

    return {
        "uid": "signal-dashboard",
        "title": "Signal",
        "description": "Signal quality monitoring — generated by generate_dashboards.py",
        "tags": [],
        "timezone": "utc",
        "editable": True,
        "time": {"from": "now-6M", "to": "now"},
        "refresh": "",
        "templating": {"list": variables},
        "graphTooltip": 1,
        "annotations": {"list": []},
        "panels": panels,
        "schemaVersion": 39,
        "version": 1,
    }


def main() -> None:
    # WHY: cloud_deploy.sh syncs provisioning/ with `rsync --delete`, which
    # only removes files no longer present in the repo — it can't tell that a
    # same-named file's uid changed. Keep each dashboard's filename in sync
    # with its uid (rename both together) so a uid change is always also a
    # file rename, and --delete actually cleans up the old uid on deploy.
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Strategy Dashboard
    dashboard = render_unified_dashboard()
    out_path = OUT_DIR / "strategy_dashboard.json"
    out_path.write_text(
        json.dumps(dashboard, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("%s — %d panels", out_path, len(dashboard["panels"]))

    # Account Overview Dashboard
    account_overview = render_account_overview_dashboard()
    account_path = OUT_DIR / "account_overview_dashboard.json"
    account_path.write_text(
        json.dumps(account_overview, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("%s — %d panels", account_path, len(account_overview["panels"]))

    # Signal Dashboard
    sig_mon = render_signal_monitor()
    sig_path = OUT_DIR / "signal_dashboard.json"
    sig_path.write_text(
        json.dumps(sig_mon, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("%s — %d panels", sig_path, len(sig_mon["panels"]))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    main()
