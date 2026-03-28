#!/usr/bin/env python3
"""Grafana Dashboard Generator.

Produces three dashboards (Backtest / Monitor / Live) from shared panel definitions.
Usage: python grafana/generate_dashboards.py
"""
from __future__ import annotations

import copy
import json
import pathlib

DATASOURCE = {"type": "grafana-postgresql-datasource", "uid": "P40AE60E18F02DE32"}
OUT_DIR = pathlib.Path(__file__).parent / "dashboards"


def _target(sql: str, ref_id: str = "A", fmt: str = "time_series") -> dict:
    return {"rawSql": sql, "format": fmt, "refId": ref_id, "datasource": DATASOURCE}


def _stat_target(sql: str) -> dict:
    return _target(sql, "A", "table")


def _kpi_stat(title: str, sql: str, unit: str | None, thresholds: list[dict]) -> dict:
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
        "_type": "kpi",
        "title": title,
        "type": "stat",
        "h": 4,
        "w": 4,
        "targets": [_stat_target(sql)],
        "fieldConfig": fc,
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "colorMode": "value",
            "graphMode": "none",
        },
    }


BASE_PANELS_DEF: list[dict] = [
    _kpi_stat(
        "Total Return",
        "SELECT total_return FROM strategy_performance WHERE run_id = '${run_id}'",
        "percentunit",
        [{"color": "red", "value": None}, {"color": "green", "value": 0}],
    ),
    _kpi_stat(
        "Max Drawdown",
        "SELECT max_drawdown FROM strategy_performance WHERE run_id = '${run_id}'",
        "percentunit",
        [{"color": "red", "value": None}],
    ),
    _kpi_stat(
        "Sharpe Ratio",
        "SELECT sharpe FROM strategy_performance WHERE run_id = '${run_id}'",
        None,
        [
            {"color": "red", "value": None},
            {"color": "yellow", "value": 0.5},
            {"color": "green", "value": 1.0},
        ],
    ),
    _kpi_stat(
        "Win Rate",
        "SELECT win_rate FROM strategy_performance WHERE run_id = '${run_id}'",
        "percentunit",
        [{"color": "red", "value": None}, {"color": "green", "value": 0.5}],
    ),
    _kpi_stat(
        "Profit Factor",
        "SELECT profit_factor FROM strategy_performance WHERE run_id = '${run_id}'",
        None,
        [
            {"color": "red", "value": None},
            {"color": "yellow", "value": 1.0},
            {"color": "green", "value": 1.5},
        ],
    ),
    _kpi_stat(
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
                {
                    "matcher": {"id": "byName", "options": "Strategy"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}}
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "Benchmark"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "orange", "mode": "fixed"}}
                    ],
                },
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
        "_type": "half",
        "title": "Trade Signals",
        "type": "timeseries",
        "h": 10,
        "w": 12,
        "targets": [
            _target(
                "SELECT ts AS time, close FROM ohlcv"
                " WHERE run_id = '${run_id}' AND $__timeFilter(ts) ORDER BY ts",
                "A",
            ),
            _target(
                "SELECT ts AS time, price AS \"Entry\" FROM strategy_signals"
                " WHERE run_id = '${run_id}' AND signal_type = 'entry' AND $__timeFilter(ts)",
                "B",
            ),
            _target(
                "SELECT ts AS time, price AS \"Exit\" FROM strategy_signals"
                " WHERE run_id = '${run_id}' AND signal_type = 'exit' AND $__timeFilter(ts)",
                "C",
            ),
        ],
        "fieldConfig": {
            "defaults": {
                "custom": {"lineWidth": 1, "showPoints": "never"}
            },
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "close"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "#5794F2", "mode": "fixed"}},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "Entry"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}},
                        {"id": "custom.lineWidth", "value": 0},
                        {"id": "custom.showPoints", "value": "always"},
                        {"id": "custom.pointSize", "value": 10},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "Exit"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}},
                        {"id": "custom.lineWidth", "value": 0},
                        {"id": "custom.showPoints", "value": "always"},
                        {"id": "custom.pointSize", "value": 10},
                    ],
                },
            ],
        },
        "options": {
            "tooltip": {"mode": "multi"},
            "legend": {"displayMode": "list", "placement": "bottom"},
        },
    },
    {
        "_type": "half",
        "title": "Trade Detail",
        "type": "table",
        "h": 10,
        "w": 12,
        "targets": [
            _target(
                "SELECT"
                " entry_ts AS \"Entry Time\","
                " exit_ts AS \"Exit Time\","
                " CASE WHEN side='buy' THEN '+' ELSE '-' END || ROUND(quantity::numeric,2) AS \"Position\","
                " ROUND(entry_price::numeric,2) AS \"Entry Price\","
                " ROUND(exit_price::numeric,2) AS \"Exit Price\","
                " holding_bars AS \"Bars\","
                " ROUND(((exit_price-entry_price)/NULLIF(entry_price,0)*100)::numeric,2) AS \"Gross Return %\","
                " ROUND((net_pnl/NULLIF(entry_price*quantity,0)*100)::numeric,2) AS \"Net Return %\""
                " FROM trade_blotter WHERE run_id = '${run_id}' ORDER BY entry_ts DESC",
                "A",
                "table",
            )
        ],
        "fieldConfig": {
            "defaults": {},
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "Gross Return %"},
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
            "sortBy": [{"displayName": "Entry Time", "desc": True}],
        },
    },
]

EXTRA_PANELS: list[dict] = [
    {
        "_type": "half",
        "title": "Unrealized PnL",
        "type": "stat",
        "h": 4,
        "w": 12,
        "targets": [_stat_target("SELECT 0 AS \"Unrealized PnL\"  -- placeholder")],
        "fieldConfig": {
            "defaults": {
                "thresholds": {
                    "mode": "absolute",
                    "steps": [{"color": "blue", "value": None}],
                },
                "color": {"mode": "thresholds"},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "colorMode": "value",
            "graphMode": "none",
        },
    },
    {
        "_type": "half",
        "title": "Current Position",
        "type": "stat",
        "h": 4,
        "w": 12,
        "targets": [_stat_target("SELECT 'N/A' AS \"Position\"  -- placeholder")],
        "fieldConfig": {
            "defaults": {
                "thresholds": {
                    "mode": "absolute",
                    "steps": [{"color": "blue", "value": None}],
                },
                "color": {"mode": "thresholds"},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "colorMode": "value",
            "graphMode": "none",
        },
    },
]


def build_panels(panel_defs: list[dict]) -> list[dict]:
    """Assign id, gridPos, datasource to panel definitions."""
    panels: list[dict] = []
    panel_id = 1
    y = 0

    kpi_defs = [p for p in panel_defs if p.get("_type") == "kpi"]
    other_defs = [p for p in panel_defs if p.get("_type") != "kpi"]

    x = 0
    for defn in kpi_defs:
        panels.append(_materialize_panel(defn, panel_id, x, y))
        panel_id += 1
        x += defn["w"]

    if kpi_defs:
        y += 4

    x = 0
    for defn in other_defs:
        ptype = defn.get("_type", "full_row")
        if ptype == "full_row":
            panels.append(_materialize_panel(defn, panel_id, 0, y))
            panel_id += 1
            y += defn["h"]
            x = 0
        elif ptype == "half":
            panels.append(_materialize_panel(defn, panel_id, x, y))
            panel_id += 1
            x += defn["w"]
            if x >= 24:
                y += defn["h"]
                x = 0

    return panels


def _materialize_panel(defn: dict, panel_id: int, x: int, y: int) -> dict:
    p = copy.deepcopy(defn)
    p.pop("_type", None)
    h = p.pop("h")
    w = p.pop("w")
    p["id"] = panel_id
    p["gridPos"] = {"h": h, "w": w, "x": x, "y": y}
    p["datasource"] = DATASOURCE
    return p


def _make_run_id_variable(mode: str) -> dict:
    sql = f"SELECT run_id FROM backtest_runs WHERE mode='{mode}' ORDER BY run_ts DESC LIMIT 20"
    return {
        "name": "run_id",
        "label": "Run ID",
        "type": "query",
        "datasource": DATASOURCE,
        "definition": sql,
        "query": sql,
        "rawQuery": True,
        "refresh": 1,
        "regex": "",
        "includeAll": False,
        "sort": 0,
        "current": {},
        "hide": 0,
        "multi": False,
    }


def render_dashboard(
    title: str,
    uid: str,
    mode: str,
    default_time: str,
    extra_panels: list[dict],
) -> dict:
    all_defs = list(BASE_PANELS_DEF) + list(extra_panels)
    panels = build_panels(all_defs)
    return {
        "uid": uid,
        "title": title,
        "description": f"{title} dashboard — generated by generate_dashboards.py",
        "tags": [],
        "timezone": "browser",
        "editable": True,
        "time": {"from": default_time, "to": "now"},
        "refresh": "5m",
        "templating": {"list": [_make_run_id_variable(mode)]},
        "annotations": {"list": []},
        "panels": panels,
        "schemaVersion": 39,
        "version": 1,
    }


DASHBOARDS = [
    ("Backtest", "backtest_dashboard", "backtest", "now-180d", []),
    ("Monitor",  "sim_dashboard",      "sim",      "now-7d",   EXTRA_PANELS),
    ("Live",     "live_dashboard",     "live",     "now-1d",   EXTRA_PANELS),
]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for title, uid, mode, default_time, extra in DASHBOARDS:
        d = render_dashboard(title, uid, mode, default_time, extra)
        out_path = OUT_DIR / f"{uid}.json"
        out_path.write_text(json.dumps(d, indent=2, ensure_ascii=False))
        print(f"✅ {out_path} — {len(d['panels'])} panels")


if __name__ == "__main__":
    main()
