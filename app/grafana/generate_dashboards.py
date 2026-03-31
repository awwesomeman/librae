#!/usr/bin/env python3
"""Grafana Dashboard Generator.

Produces a single unified Strategy Dashboard with mode filtering.
Usage: python app/grafana/generate_dashboards.py
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
) -> dict:
    """Build a Grafana stat panel definition."""
    fc: dict = {
        "defaults": {
            "thresholds": {"mode": "absolute", "steps": thresholds},
            "color": {"mode": "thresholds"},
        },
        "overrides": [],
    }
    if unit:
        fc["defaults"]["unit"] = unit
    return {
        "_type": layout,
        "title": title,
        "type": "stat",
        "h": 4,
        "w": w,
        "targets": [_stat_target(sql)],
        "fieldConfig": fc,
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "colorMode": "value",
            "graphMode": "none",
        },
    }


# WHY: Returns integer for Grafana value mapping: 1=Online, 0=Offline, -1=N/A (no heartbeat).
# Threshold = 2x strategy timeframe (not poll_interval) to avoid false Offline on brief delays.
_STATUS_SQL = (
    "SELECT CASE"
    " WHEN last_heartbeat IS NULL THEN -1"
    " WHEN last_heartbeat > now() - "
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
    "type": "stat",
    "h": 4,
    "w": 4,
    "targets": [_stat_target(_STATUS_SQL)],
    "fieldConfig": {
        "defaults": {
            "mappings": [
                {"type": "value", "options": {
                    "1": {"text": "Online", "color": "green", "index": 0},
                    "0": {"text": "Offline", "color": "red", "index": 1},
                    "-1": {"text": "-", "color": "text", "index": 2},
                }},
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

BASE_PANELS_DEF: list[dict] = [
    {"_type": "row", "title": "Performance Overview"},
    _stat_panel(
        "Total Return",
        "SELECT total_return FROM strategy_performance WHERE run_id = '${run_id}'",
        "percentunit",
        [{"color": "red", "value": None}, {"color": "green", "value": 0}],
    ),
    _stat_panel(
        "Max Drawdown",
        "SELECT max_drawdown FROM strategy_performance WHERE run_id = '${run_id}'",
        "percentunit",
        [{"color": "red", "value": None}],
    ),
    _stat_panel(
        "Sharpe Ratio",
        "SELECT sharpe FROM strategy_performance WHERE run_id = '${run_id}'",
        None,
        [
            {"color": "red", "value": None},
            {"color": "yellow", "value": 0.5},
            {"color": "green", "value": 1.0},
        ],
    ),
    _stat_panel(
        "Win Rate",
        "SELECT win_rate FROM strategy_performance WHERE run_id = '${run_id}'",
        "percentunit",
        [{"color": "red", "value": None}, {"color": "green", "value": 0.5}],
    ),
    _stat_panel(
        "Profit Factor",
        "SELECT profit_factor FROM strategy_performance WHERE run_id = '${run_id}'",
        None,
        [
            {"color": "red", "value": None},
            {"color": "yellow", "value": 1.0},
            {"color": "green", "value": 1.5},
        ],
    ),
    _stat_panel(
        "Trades",
        "SELECT trades FROM strategy_performance WHERE run_id = '${run_id}'",
        None,
        [{"color": "blue", "value": None}],
    ),
    {
        "_type": "half",
        "title": "Equity Curve",
        "type": "timeseries",
        "h": 8,
        "w": 12,
        "targets": [
            _target(
                "SELECT ts AS time, equity AS \"Strategy\", benchmark_equity AS \"Benchmark\""
                " FROM equity_curve WHERE run_id = '${run_id}' AND $__timeFilter(ts) ORDER BY ts"
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
                _color_override("Benchmark", "orange"),
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
        "type": "timeseries",
        "h": 8,
        "w": 12,
        "targets": [
            _target(
                "SELECT ts AS time, drawdown AS \"Drawdown %\""
                " FROM equity_curve WHERE run_id = '${run_id}' AND $__timeFilter(ts) ORDER BY ts"
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
        "_type": "fixed", "_x": 0, "_dy": 0,
        "title": "Price Trend",
        "type": "timeseries",
        "h": 10,
        "w": 12,
        "targets": [
            _target(
                "SELECT ts AS time, close FROM ohlcv"
                " WHERE run_id = '${run_id}' AND $__timeFilter(ts) ORDER BY ts",
                "A",
            ),
        ],
        "fieldConfig": {
            "defaults": {
                "custom": {"lineWidth": 1, "showPoints": "never"}
            },
            "overrides": [_color_override("close", "#5794F2")],
        },
        "options": {
            "tooltip": {"mode": "single"},
            "legend": {"displayMode": "list", "placement": "bottom"},
        },
    },
    {
        "_type": "fixed", "_x": 12, "_dy": 0,
        "title": "Trade Detail",
        "type": "table",
        "h": 15,
        "w": 12,
        "targets": [
            _target(
                "SELECT"
                " ROW_NUMBER() OVER (ORDER BY entry_ts) AS \"#\","
                " entry_ts AS \"Entry Time\","
                " exit_ts AS \"Exit Time\","
                " CASE WHEN side='long' THEN ROUND(quantity::numeric,4)"
                " ELSE -ROUND(quantity::numeric,4) END AS \"Qty\","
                " ROUND(entry_price::numeric,2) AS \"Entry Price\","
                " ROUND(exit_price::numeric,2) AS \"Exit Price\","
                " ROUND(gross_return::numeric,2) AS \"Gross Return %\","
                " ROUND(net_return::numeric,2) AS \"Net Return %\","
                " holding_bars AS \"Periods\","
                " SPLIT_PART(trade_id, '-t', 2)::int AS \"Order ID\""
                " FROM trade_blotter WHERE run_id = '${run_id}'"
                " AND ($__timeFilter(entry_ts) OR $__timeFilter(exit_ts))"
                " ORDER BY entry_ts",
                "A",
                "table",
            )
        ],
        "fieldConfig": {
            "defaults": {},
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "Net Return %"},
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
                }
            ],
        },
        "options": {
            "showHeader": True,
            "sortBy": [{"displayName": "Entry Time", "desc": False}],
        },
    },
    {
        "_type": "fixed", "_x": 0, "_dy": 10,
        "title": "Entry / Exit Signals",
        "type": "timeseries",
        "h": 5,
        "w": 12,
        "targets": [
            _target(
                "SELECT ts AS time, price AS \"Entry\" FROM strategy_signals"
                " WHERE run_id = '${run_id}' AND signal_type = 'entry' AND $__timeFilter(ts)",
                "A",
            ),
            _target(
                "SELECT ts AS time, price AS \"Exit\" FROM strategy_signals"
                " WHERE run_id = '${run_id}' AND signal_type = 'exit' AND $__timeFilter(ts)",
                "B",
            ),
        ],
        "fieldConfig": {
            "defaults": {
                "custom": {"lineWidth": 0, "showPoints": "always", "pointSize": 12}
            },
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

EXTRA_PANELS: list[dict] = [
    {"_type": "row", "title": "Live / Sim Only"},
    {**STATUS_PANEL, "w": 8},
    _stat_panel(
        "Unrealized PnL",
        "SELECT 0 AS \"Unrealized PnL\"  -- TODO: replace placeholder",
        None,
        [{"color": "blue", "value": None}],
        w=8,
    ),
    _stat_panel(
        "Current Position",
        "SELECT 'N/A' AS \"Position\"  -- TODO: replace placeholder",
        None,
        [{"color": "blue", "value": None}],
        w=8,
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
    csv = ",".join(value for _, value in options)
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
    name: str, sql: str, *, hide: int = 0, label: str | None = None,
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
    run_id_var = _make_query_variable(
        "run_id",
        "SELECT run_id FROM backtest_runs WHERE mode='${mode}'"
        " ORDER BY run_ts DESC LIMIT 20",
        label="Run ID",
    )

    return {
        "uid": "strategy_dashboard",
        "title": "Strategy Dashboard",
        "description": "Unified strategy dashboard — generated by generate_dashboards.py",
        "tags": [],
        "timezone": "browser",
        "editable": True,
        "time": {"from": "now-1y", "to": "now"},
        "refresh": "5m",
        "templating": {
            "list": [mode_var, run_id_var],
        },
        "graphTooltip": 1,
        "annotations": {"list": []},
        "panels": panels,
        "schemaVersion": 39,
        "version": 1,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dashboard = render_unified_dashboard()
    out_path = OUT_DIR / "strategy_dashboard.json"
    out_path.write_text(json.dumps(dashboard, indent=2, ensure_ascii=False))
    logger.info("%s — %d panels", out_path, len(dashboard["panels"]))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
