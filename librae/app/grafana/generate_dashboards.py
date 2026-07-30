#!/usr/bin/env python3
"""Grafana Dashboard Generator.

Produces a single unified Strategy Dashboard with mode filtering.
Usage: python -m librae.app.grafana.generate_dashboards
"""

from __future__ import annotations

import copy
import json
import logging
import pathlib

logger = logging.getLogger(__name__)

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


def _mapping_override(
    name: str, mappings: dict[str, str], cell_mode: str = "color-background"
) -> dict:
    """Build a Grafana field override with value mappings and cell coloring."""
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [
            {
                "id": "mappings",
                "value": [
                    {
                        "type": "value",
                        "options": {
                            k: {"color": v, "index": i} for i, (k, v) in enumerate(mappings.items())
                        },
                    },
                ],
            },
            {"id": "custom.cellOptions", "value": {"type": cell_mode}},
        ],
    }


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
        " WHERE run_id = '${run_id}' AND account_id = '${account_id}'"
    )


# WHY: Returns integer for Grafana value mapping: 1=Online, 0=Offline, -1=N/A (no heartbeat).
# Threshold = 2x strategy timeframe (not poll_seconds) to avoid false Offline on brief delays.
_STATUS_SQL = (
    "SELECT CASE"
    " WHEN last_heartbeat_at IS NULL THEN -1"
    " WHEN last_heartbeat_at > now() - "
    "CASE UPPER(timeframe)"
    " WHEN 'H1' THEN interval '2 hours'"
    " WHEN '1H' THEN interval '2 hours'"
    " WHEN 'M5' THEN interval '10 minutes'"
    " WHEN '5M' THEN interval '10 minutes'"
    " WHEN 'M15' THEN interval '30 minutes'"
    " WHEN '15M' THEN interval '30 minutes'"
    " WHEN 'H4' THEN interval '8 hours'"
    " WHEN '4H' THEN interval '8 hours'"
    " WHEN 'D1' THEN interval '2 days'"
    " WHEN '1D' THEN interval '2 days'"
    " ELSE interval '2 hours'"
    " END"
    " THEN 1"
    " ELSE 0"
    " END AS status"
    " FROM backtest_runs WHERE run_id = '${run_id}'"
)

STATUS_PANEL: dict = {
    "_type": "kpi",
    "title": "Status",
    "description": "Online if last_heartbeat_at within 2x timeframe. Offline = process may have stopped.",
    "type": "stat",
    "h": 4,
    "w": 4,
    "targets": [_stat_target(_STATUS_SQL)],
    "fieldConfig": {
        "defaults": {
            "mappings": [
                {
                    "type": "value",
                    "options": {
                        "1": {"text": "Online", "color": "green", "index": 0},
                        "0": {"text": "Offline", "color": "red", "index": 1},
                        "-1": {"text": "-", "color": "text", "index": 2},
                    },
                },
            ],
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

# ---------------------------------------------------------------------------
# KPI catalogue — all available stat panels for Performance Overview.
# Edit DEFAULT_KPIS to control which KPIs appear on the dashboard.
# ---------------------------------------------------------------------------

_KPI_CATALOGUE: dict[str, dict] = {
    "total_return": _stat_panel(
        "Total Return %",
        _account_metric_sql("total_return"),
        "percentunit",
        [{"color": "red", "value": None}, {"color": "green", "value": 0}],
        description="Compounded return over the full stored sample. Not annualized.",
    ),
    "max_drawdown": _stat_panel(
        "Max Drawdown %",
        _account_metric_sql("max_drawdown"),
        "percentunit",
        [{"color": "red", "value": None}],
        description="Negative ratio: largest peak-to-trough decline.",
    ),
    "period_sharpe": _stat_panel(
        "Period Sharpe",
        _account_metric_sql("period_sharpe"),
        None,
        [
            {"color": "red", "value": None},
            {"color": "green", "value": 0},
        ],
        description="Mean period return / sample period volatility. Not annualized; compare only like-frequency observations.",
    ),
    "period_sortino": _stat_panel(
        "Period Sortino",
        _account_metric_sql("period_sortino"),
        None,
        [
            {"color": "red", "value": None},
            {"color": "green", "value": 0},
        ],
        description="Mean period return / period downside deviation. Not annualized.",
    ),
    "win_rate": _stat_panel(
        "Win Rate %",
        _account_metric_sql("win_rate"),
        "percentunit",
        [{"color": "red", "value": None}, {"color": "green", "value": 0.5}],
        description="Winning trades (net_pnl > 0) / total trades. Interpret with PF — low win rate + high PF = trend following.",
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
        description="sum(winning net_pnl) / sum(losing net_pnl). None if no losses. >1.5 healthy, <1.0 losing.",
    ),
    "trades": _stat_panel(
        "Trades",
        _account_metric_sql("trades"),
        None,
        [{"color": "blue", "value": None}],
        description="Total closed trades (reduce + close). Low count (<30) = metrics statistically unreliable.",
    ),
    "avg_trade_return": _stat_panel(
        "Avg Trade Return %",
        _account_metric_sql("avg_trade_return"),
        "percentunit",
        [{"color": "red", "value": None}, {"color": "green", "value": 0}],
        description="Notional-weighted mean net return per realized exit.",
    ),
    "exposure_ratio": _stat_panel(
        "Exposure %",
        _account_metric_sql("exposure_ratio"),
        "percentunit",
        [{"color": "blue", "value": None}],
        description="Bars with any open position / total bars. Multi-asset safe — overlapping positions counted once.",
    ),
    "mean_period_return": _stat_panel(
        "Mean Period Return %",
        _account_metric_sql("mean_period_return"),
        "percentunit",
        [{"color": "red", "value": None}, {"color": "green", "value": 0}],
        description="Arithmetic mean return per stored observation. Not annualized.",
    ),
    "positive_period_rate": _stat_panel(
        "Positive Periods %",
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
    "win_rate",
    "profit_factor",
    "trades",
]

BASE_PANELS_DEF: list[dict] = [
    {"_type": "row", "title": "Performance Overview"},
    *[_KPI_CATALOGUE[k] for k in DEFAULT_KPIS],
    {
        "_type": "half",
        "title": "Equity Curve",
        "description": "Portfolio equity over time.",
        "type": "timeseries",
        "h": 8,
        "w": 12,
        "targets": [
            _target(
                'SELECT ts AS time, equity AS "Strategy"'
                " FROM equity_curve WHERE run_id = '${run_id}'"
                " AND account_id = '${account_id}' AND $__timeFilter(ts) ORDER BY ts"
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
        "_type": "half",
        "title": "Drawdown %",
        "description": "Peak-to-trough drawdown over time. Depth = risk, duration = recovery speed.",
        "type": "timeseries",
        "h": 8,
        "w": 12,
        "targets": [
            _target(
                'SELECT ts AS time, drawdown AS "Drawdown %"'
                " FROM equity_curve WHERE run_id = '${run_id}'"
                " AND account_id = '${account_id}' AND $__timeFilter(ts) ORDER BY ts"
            )
        ],
        "fieldConfig": {
            "defaults": {
                "unit": "percentunit",
                "max": 0,
                "custom": {"lineWidth": 1, "fillOpacity": 30, "showPoints": "never"},
                "color": {"fixedColor": "red", "mode": "fixed"},
            },
            "overrides": [],
        },
        "options": {
            "tooltip": {"mode": "single"},
            "legend": {"displayMode": "list", "placement": "bottom"},
        },
    },
    {
        "_type": "fixed",
        "_x": 0,
        "_dy": 0,
        "title": "Price Trend",
        "description": "Close price for every run symbol, matched by timeframe and data source.",
        "type": "timeseries",
        "h": 10,
        "w": 12,
        "targets": [
            _target(
                "WITH meta AS ("
                " SELECT symbols, timeframe, data_source, started_at, ended_at"
                " FROM backtest_runs WHERE run_id = '${run_id}')"
                " SELECT o.ts AS time, o.close, o.symbol AS metric"
                " FROM ohlcv o, meta m"
                " WHERE o.symbol IN (SELECT jsonb_array_elements_text(m.symbols))"
                " AND o.timeframe = m.timeframe"
                " AND (m.data_source IS NULL OR o.data_source = m.data_source)"
                " AND (m.started_at IS NULL OR o.ts >= m.started_at)"
                " AND (m.ended_at IS NULL OR o.ts <= m.ended_at)"
                " AND $__timeFilter(o.ts)"
                " ORDER BY o.ts",
                "A",
            ),
        ],
        "fieldConfig": {
            "defaults": {"custom": {"lineWidth": 1, "showPoints": "never"}},
            "overrides": [_color_override("close", "#5794F2")],
        },
        "options": {
            "tooltip": {"mode": "single"},
            "legend": {"displayMode": "list", "placement": "bottom"},
        },
    },
    # -- Trade Events (single table replacing old Trade Detail) --
    {
        "_type": "fixed",
        "_x": 12,
        "_dy": 0,
        "title": "Trade Events",
        "description": (
            "Position lifecycle: open/add = entry side, reduce/close = exit side.\n"
            "P&L and return shown on reduce/close rows only (weighted-average entry price).\n"
            "One full lifecycle = remaining_quantity goes from 0 → N → 0."
        ),
        "type": "table",
        "h": 15,
        "w": 12,
        "targets": [
            _target(
                "SELECT"
                ' ROW_NUMBER() OVER (ORDER BY ts) AS "#",'
                ' ts AS "Time",'
                ' account_id AS "Account",'
                ' currency AS "Currency",'
                ' event_type AS "Event",'
                ' symbol AS "Symbol",'
                ' side AS "Side",'
                ' ROUND(fill_quantity::numeric,4) AS "Qty",'
                ' ROUND(price::numeric,2) AS "Price",'
                ' ROUND(entry_price::numeric,2) AS "Avg Entry",'
                ' ROUND(remaining_quantity::numeric,4) AS "Pos Qty",'
                ' ROUND((commission + slippage + tax)::numeric,2) AS "Cost",'
                ' ROUND(pnl::numeric,2) AS "Net P&L",'
                ' ROUND(net_return::numeric,2) AS "Net Return %",'
                ' entry_at AS "Entry Time",'
                ' periods_held AS "Periods",'
                ' reason AS "Reason"'
                " FROM trade_events WHERE run_id = '${run_id}'"
                " AND account_id = '${account_id}'"
                " AND $__timeFilter(ts)"
                " ORDER BY ts",
                "A",
                "table",
            )
        ],
        "fieldConfig": {
            "defaults": {},
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "Net P&L"},
                    "properties": [
                        {
                            "id": "thresholds",
                            "value": {
                                "mode": "absolute",
                                "steps": [
                                    {"color": "red", "value": None},
                                    {"color": "green", "value": 0},
                                ],
                            },
                        },
                        {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                    ],
                },
                _mapping_override(
                    "Event",
                    {
                        "open": "blue",
                        "add": "super-light-blue",
                        "reduce": "orange",
                        "close": "semi-dark-orange",
                    },
                ),
                _mapping_override("Side", {"long": "green", "short": "red"}, "color-text"),
                {
                    "matcher": {"id": "byName", "options": "Net Return %"},
                    "properties": [
                        {"id": "unit", "value": "percent"},
                    ],
                },
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
        "title": "Entry / Exit Signals",
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
                " FROM trade_events te"
                " JOIN backtest_runs br ON br.run_id = te.run_id"
                " WHERE te.run_id = '${run_id}'"
                " AND te.account_id = '${account_id}'"
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
                " FROM trade_events te"
                " JOIN backtest_runs br ON br.run_id = te.run_id"
                " WHERE te.run_id = '${run_id}'"
                " AND te.account_id = '${account_id}'"
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
]


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


EXTRA_PANELS: list[dict] = [
    {"_type": "row", "title": "Live / Sim Only"},
    {**STATUS_PANEL, "w": 6},
    _poll_seconds_panel(w=6),
    _stat_panel(
        "Unrealized PnL",
        (
            "WITH meta AS ("
            " SELECT timeframe, data_source"
            " FROM backtest_runs WHERE run_id='${run_id}'"
            "),\n"
            "pos AS (\n"
            "  SELECT symbol, side, entry_price, remaining_quantity\n"
            "  FROM trade_events\n"
            "  WHERE run_id = '${run_id}'\n"
            "    AND account_id = '${account_id}'\n"
            "  ORDER BY ts DESC LIMIT 1\n"
            "),\n"
            "latest AS (\n"
            "  SELECT close FROM ohlcv, meta, pos\n"
            "  WHERE ohlcv.symbol = pos.symbol AND ohlcv.timeframe = meta.timeframe\n"
            "    AND (meta.data_source IS NULL OR ohlcv.data_source = meta.data_source)\n"
            "  ORDER BY ts DESC LIMIT 1\n"
            ")\n"
            "SELECT CASE WHEN p.remaining_quantity > 0 THEN\n"
            "  CASE WHEN p.side='long'\n"
            "    THEN (l.close - p.entry_price) * p.remaining_quantity\n"
            "    ELSE (p.entry_price - l.close) * p.remaining_quantity\n"
            '  END ELSE NULL END AS "PnL"\n'
            "FROM pos p, latest l"
        ),
        None,
        [
            {"color": "red", "value": None},
            {"color": "red", "value": -100},
            {"color": "yellow", "value": 0},
            {"color": "green", "value": 100},
        ],
        w=6,
        decimals=2,
        no_value="No Position",
        description="Unrealized P&L of the current open position.",
    ),
    _stat_panel(
        "Current Position",
        (
            "WITH pos AS (\n"
            "  SELECT side, remaining_quantity, entry_price\n"
            "  FROM trade_events\n"
            "  WHERE run_id = '${run_id}'\n"
            "    AND account_id = '${account_id}'\n"
            "  ORDER BY ts DESC LIMIT 1\n"
            ")\n"
            "SELECT CASE WHEN remaining_quantity > 0 THEN\n"
            "  CASE WHEN side='long' THEN '+' ELSE '-' END\n"
            "  || ROUND(remaining_quantity::numeric, 4) || ' @ '\n"
            "  || ROUND(entry_price::numeric, 2)\n"
            'ELSE NULL END AS "Position"\n'
            "FROM pos"
        ),
        None,
        [],
        w=6,
        no_value="Flat",
        fixed_color="blue",
        description="Current open position: direction, size, entry price.",
    ),
]


def build_panels(panel_defs: list[dict]) -> list[dict]:
    """Assign id, gridPos, datasource to panel definitions in definition order."""
    panels: list[dict] = []
    panel_id = 1
    x = 0
    y = 0
    row_h = 0  # tallest panel in the current row

    fixed_defs: list[dict] = []
    valid_types = {"kpi", "half", "fixed", "row", "full_row"}
    for defn in panel_defs:
        ptype = defn.get("_type")
        if ptype is None:
            raise ValueError(f"Missing _type in panel {defn.get('title', '?')!r}")
        if ptype not in valid_types:
            raise ValueError(f"Unknown panel _type: {ptype!r} in panel {defn.get('title', '?')!r}")
        if ptype == "fixed":
            fixed_defs.append(defn)
            continue

        # WHY: flush incomplete row before block-level panels (row / full_row)
        if ptype in ("row", "full_row") and x > 0:
            y += row_h
            x = 0
            row_h = 0

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
) -> dict:
    """Build a Grafana query-type template variable."""
    v: dict = {
        "name": name,
        "type": "query",
        "datasource": DATASOURCE,
        "definition": sql,
        "query": sql,
        "rawQuery": True,
        "refresh": 2,
        "regex": "",
        "includeAll": False,
        "sort": 0,
        "current": {},
        "hide": hide,
        "multi": False,
    }
    if label:
        v["label"] = label
    return v


def render_unified_dashboard() -> dict:
    """Build the single unified Strategy Dashboard."""
    all_defs = list(EXTRA_PANELS) + list(BASE_PANELS_DEF)
    panels = build_panels(all_defs)

    mode_var = _make_custom_variable(
        "mode",
        [("Backtest", "backtest"), ("Sim", "sim"), ("Live", "live")],
        label="Mode",
    )
    strategy_var = _make_query_variable(
        "strategy",
        "SELECT DISTINCT strategy FROM backtest_runs WHERE mode='${mode}' ORDER BY strategy",
        label="Strategy",
    )
    run_id_var = _make_query_variable(
        "run_id",
        "SELECT run_id FROM backtest_runs"
        " WHERE mode='${mode}' AND strategy='${strategy}'"
        " AND run_id IN (SELECT run_id FROM strategy_performance)"
        " ORDER BY run_at DESC LIMIT 20",
        label="Run ID",
    )
    account_id_var = _make_query_variable(
        "account_id",
        "SELECT account_id FROM strategy_performance WHERE run_id='${run_id}' ORDER BY account_id",
        label="Account",
    )

    return {
        "uid": "strategy_dashboard",
        "title": "Strategy",
        "description": "Unified strategy dashboard — generated by generate_dashboards.py",
        "tags": [],
        "timezone": "browser",
        "editable": True,
        "time": {"from": "now-1y", "to": "now"},
        "refresh": "5m",
        "templating": {
            "list": [mode_var, strategy_var, run_id_var, account_id_var],
        },
        "graphTooltip": 1,
        "annotations": {"list": []},
        "panels": panels,
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
    " AND (meta.data_source IS NULL OR ohlcv.data_source = meta.data_source)"
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
            "strategy",
            "SELECT DISTINCT strategy FROM backtest_runs WHERE mode='${mode}' ORDER BY strategy",
            label="Strategy",
        ),
        _make_query_variable(
            "run_id",
            "SELECT run_id FROM backtest_runs"
            " WHERE mode='${mode}' AND strategy='${strategy}'"
            " AND run_id IN (SELECT DISTINCT run_id FROM signal_events WHERE run_id IS NOT NULL)"
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
        "uid": "signal-monitor",
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
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Strategy Dashboard
    dashboard = render_unified_dashboard()
    out_path = OUT_DIR / "strategy_dashboard.json"
    out_path.write_text(json.dumps(dashboard, indent=2, ensure_ascii=False))
    logger.info("%s — %d panels", out_path, len(dashboard["panels"]))

    # Signal Monitor
    sig_mon = render_signal_monitor()
    sig_path = OUT_DIR / "signal_monitor.json"
    sig_path.write_text(json.dumps(sig_mon, indent=2, ensure_ascii=False))
    logger.info("%s — %d panels", sig_path, len(sig_mon["panels"]))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    main()
