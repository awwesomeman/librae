"""Tests for Grafana dashboard generation."""
from __future__ import annotations

from app.grafana.generate_dashboards import (
    render_signal_monitor,
    render_unified_dashboard,
)


class TestRenderUnifiedDashboard:
    def test_panel_count(self):
        d = render_unified_dashboard()
        assert len(d["panels"]) == 16

    def test_has_required_fields(self):
        d = render_unified_dashboard()
        assert d["uid"] == "strategy_dashboard"
        assert "templating" in d
        assert "panels" in d
        assert d["schemaVersion"] == 39

    def test_variables(self):
        d = render_unified_dashboard()
        var_names = [v["name"] for v in d["templating"]["list"]]
        assert "mode" in var_names
        assert "run_id" in var_names

    def test_no_strategy_signals_references(self):
        """Ensure no panel SQL references the deleted strategy_signals table."""
        import json
        raw = json.dumps(render_unified_dashboard())
        assert "strategy_signals" not in raw


class TestRenderSignalMonitor:
    def test_panel_count(self):
        d = render_signal_monitor()
        # 2 rows + 7 stat + 4 timeseries = 13
        assert len(d["panels"]) == 13

    def test_has_required_fields(self):
        d = render_signal_monitor()
        assert d["uid"] == "signal-monitor-v2"
        assert d["schemaVersion"] == 39
        assert "signal" in d["tags"]

    def test_variables(self):
        d = render_signal_monitor()
        var_names = [v["name"] for v in d["templating"]["list"]]
        assert "strategy" in var_names
        assert "symbol" in var_names
        assert "timeframe" in var_names
        assert "mode" in var_names
        assert "data_source" in var_names
        assert "n" in var_names
        assert "k" in var_names
        assert "expected_direction" in var_names

    def test_stat_panels_have_targets(self):
        d = render_signal_monitor()
        stat_panels = [p for p in d["panels"] if p["type"] == "stat"]
        assert len(stat_panels) == 7
        for p in stat_panels:
            assert len(p["targets"]) >= 1
            assert "rawSql" in p["targets"][0]

    def test_timeseries_panels_query_signal_events(self):
        d = render_signal_monitor()
        ts_panels = [p for p in d["panels"] if p["type"] == "timeseries"]
        assert len(ts_panels) == 4
        for p in ts_panels:
            sqls = [t["rawSql"] for t in p["targets"]]
            combined = " ".join(sqls)
            assert "signal_events" in combined or "ohlcv" in combined

    def test_no_hardcoded_datasource_uid_in_panels(self):
        """All panels should get datasource from build_panels, not hardcoded."""
        d = render_signal_monitor()
        for p in d["panels"]:
            if p["type"] == "row":
                continue
            assert "datasource" in p

    def test_snapshot_row_layout(self):
        """7 stat panels should fit in one row (total width = 24)."""
        d = render_signal_monitor()
        stat_panels = [p for p in d["panels"] if p["type"] == "stat"]
        total_width = sum(p["gridPos"]["w"] for p in stat_panels)
        assert total_width == 24

    def test_price_signals_panel_has_two_targets(self):
        d = render_signal_monitor()
        price_panel = next(p for p in d["panels"] if p["title"] == "Price & Signals")
        assert len(price_panel["targets"]) == 2
