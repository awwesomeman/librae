"""Tests for Grafana dashboard generation."""

from __future__ import annotations

import numpy as np
from librae.app.grafana.generate_dashboards import (
    render_signal_monitor,
    render_unified_dashboard,
)
from tests.signal_outcome_contract import (
    SIGNAL_OUTCOME_LONG_FRACTIONS,
    make_signal_outcome_contract_ohlcv,
)


class TestRenderUnifiedDashboard:
    def test_panel_count(self):
        d = render_unified_dashboard()
        assert len(d["panels"]) == 17

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
        assert "account_id" in var_names

    def test_accounting_panels_filter_the_selected_account(self):
        d = render_unified_dashboard()
        accounting_tables = ("strategy_performance", "equity_curve", "trade_events")
        for panel in d["panels"]:
            for target in panel.get("targets", []):
                sql = target["rawSql"]
                if any(table in sql for table in accounting_tables):
                    assert "${account_id}" in sql

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
        assert d["uid"] == "signal-monitor"
        assert d["schemaVersion"] == 39

    def test_variables(self):
        d = render_signal_monitor()
        var_names = [v["name"] for v in d["templating"]["list"]]
        assert "mode" in var_names
        assert "run_id" in var_names
        assert "n" in var_names
        assert "k" in var_names
        assert "signal_type" in var_names
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
        """Stat panels should fit in one row (total width = 24)."""
        d = render_signal_monitor()
        stat_panels = [p for p in d["panels"] if p["type"] == "stat"]
        total_width = sum(p["gridPos"]["w"] for p in stat_panels)
        assert total_width == 24

    def test_price_signals_panel_has_two_targets(self):
        d = render_signal_monitor()
        price_panel = next(p for p in d["panels"] if p["title"] == "Price & Signals")
        assert len(price_panel["targets"]) == 2

    def test_signal_event_and_expected_direction_are_independent(self):
        import json

        raw = json.dumps(render_signal_monitor())
        assert "s.signal_type = '${signal_type}'" in raw
        assert "CASE WHEN ${expected_direction}" not in raw

    def test_forward_return_is_direction_adjusted_once(self):
        dashboard = render_signal_monitor()
        mean_panel = next(p for p in dashboard["panels"] if p["title"] == "Mean Fwd Return (T+$n)")
        sql = mean_panel["targets"][0]["rawSql"]
        assert "$expected_direction * (exit_bar.close - entry_bar.entry_price)" in sql
        assert 'SELECT AVG(ret) AS "Mean Ret"' in sql

    def test_excursion_sql_uses_non_negative_magnitudes(self):
        dashboard = render_signal_monitor()
        edge_panel = next(p for p in dashboard["panels"] if p["title"] == "Edge Ratio (T+$n)")
        sql = edge_panel["targets"][0]["rawSql"]
        assert sql.count("MAX(GREATEST(0.0") == 2

    def test_golden_fixture_matches_grafana_fraction_contract(self):
        ohlcv = make_signal_outcome_contract_ohlcv()
        reference_price = float(ohlcv.iloc[1]["open"])
        forward = ohlcv.iloc[2:5]

        returns = (forward["close"].to_numpy() - reference_price) / reference_price
        mfe = np.maximum.accumulate(
            np.maximum(0.0, (forward["high"].to_numpy() - reference_price) / reference_price)
        )
        mae = np.maximum.accumulate(
            np.maximum(0.0, (reference_price - forward["low"].to_numpy()) / reference_price)
        )

        assert np.allclose(returns, SIGNAL_OUTCOME_LONG_FRACTIONS["forward_return"])
        assert np.allclose(mfe, SIGNAL_OUTCOME_LONG_FRACTIONS["mfe"])
        assert np.allclose(mae, SIGNAL_OUTCOME_LONG_FRACTIONS["mae"])
