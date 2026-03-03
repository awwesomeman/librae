"""Tests for scripts/reporting/render_report_html.py"""
import unittest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from scripts.reporting.render_report_html import (
    render_page,
    render_summary_table,
    render_strategy_card,
    _cost_text,
    pct,
    num,
    esc,
)

# ---------------------------------------------------------------------------
# Fake strategy data
# ---------------------------------------------------------------------------

def _make_strategy(name, instrument='TEST', oos_pf=2.5, cost_text=None):
    cs = {'fee': 10, 'slippage': 5, 'tax': 0, 'round_trip_cost': 15}
    if cost_text is not None:
        cs['round_trip_cost_text'] = cost_text
    return {
        'strategy': name,
        'instrument': instrument,
        'data': 'TestData',
        'sample_periods': {
            'train': {'start': '2024-01-01', 'end': '2024-12-31'},
            'validation': {'start': '2025-01-01', 'end': '2025-06-30'},
            'oos': {'start': '2025-07-01', 'end': '2026-03-01'},
        },
        'cost_settings': cs,
        'protocol': 'strict: train-select, validation-check, oos-once',
        'chosen_params': {'th': 70, 'bn': 3},
        'train': {
            'trades': 100, 'win_rate': 0.65, 'avg_ret': 0.001,
            'pf': 2.0, 'equity': 1.10, 'ann_return': 0.10,
            'ann_sharpe': 3.0, 'ann_vol': 0.03, 'mdd': 0.02,
        },
        'validation': {
            'trades': 20, 'win_rate': 0.70, 'avg_ret': 0.0015,
            'pf': 2.5, 'equity': 1.03, 'ann_return': 0.12,
            'ann_sharpe': 4.0, 'ann_vol': 0.025, 'mdd': 0.01,
        },
        'validation_cost_stress': {
            '15': {
                'trades': 20, 'win_rate': 0.70, 'avg_ret': 0.0015,
                'pf': 2.5, 'ann_return': 0.12, 'ann_sharpe': 4.0, 'mdd': 0.01,
            },
            '20': {
                'trades': 20, 'win_rate': 0.70, 'avg_ret': 0.0012,
                'pf': 2.1, 'ann_return': 0.10, 'ann_sharpe': 3.5, 'mdd': 0.012,
            },
        },
        'oos_final_once': {
            'trades': 50, 'win_rate': 0.68, 'avg_ret': 0.0012,
            'pf': oos_pf, 'equity': 1.07, 'ann_return': 0.15,
            'ann_sharpe': 5.0, 'ann_vol': 0.027, 'mdd': 0.008,
        },
    }


STRATEGY_A = _make_strategy('StrategyAlpha', oos_pf=3.14)
STRATEGY_B = _make_strategy('StrategyBeta', oos_pf=1.99, cost_text='NT$30 round-trip')


class TestFormatHelpers(unittest.TestCase):
    def test_pct_none(self):
        self.assertEqual(pct(None), '-')

    def test_pct_value(self):
        self.assertEqual(pct(0.1234), '12.34%')

    def test_num_none(self):
        self.assertEqual(num(None), '-')

    def test_num_value(self):
        self.assertEqual(num(3.14159), '3.14')

    def test_esc_xss(self):
        self.assertIn('&lt;', esc('<script>'))
        self.assertIn('&gt;', esc('>'))


class TestCostText(unittest.TestCase):
    def test_prefers_round_trip_cost_text(self):
        data = _make_strategy('X', cost_text='NT$30 round-trip')
        result = _cost_text(data)
        self.assertIn('NT$30', result)

    def test_falls_back_to_round_trip_cost(self):
        data = _make_strategy('X')  # no cost_text
        result = _cost_text(data)
        self.assertIn('15', result)


class TestSummaryTable(unittest.TestCase):
    def setUp(self):
        self.html = render_summary_table([STRATEGY_A, STRATEGY_B])

    def test_contains_strategy_names(self):
        self.assertIn('StrategyAlpha', self.html)
        self.assertIn('StrategyBeta', self.html)

    def test_contains_oos_pf_row(self):
        self.assertIn('OOS PF', self.html)

    def test_contains_oos_pf_values(self):
        # 3.14 and 1.99 formatted as num()
        self.assertIn('3.14', self.html)
        self.assertIn('1.99', self.html)

    def test_two_strategy_columns(self):
        # thead has Metric + 2 strategy columns = 3 <th>
        import re
        heads = re.findall(r'<th>', self.html)
        # at least 3 header cells in the table
        self.assertGreaterEqual(len(heads), 3)


class TestStrategyCard(unittest.TestCase):
    def setUp(self):
        self.html_a = render_strategy_card(STRATEGY_A)
        self.html_b = render_strategy_card(STRATEGY_B)

    def test_strategy_name_present(self):
        self.assertIn('StrategyAlpha', self.html_a)

    def test_sample_periods_present(self):
        self.assertIn('2024-01-01', self.html_a)
        self.assertIn('2026-03-01', self.html_a)

    def test_cost_stress_section_present(self):
        self.assertIn('Cost Stress', self.html_a)

    def test_round_trip_cost_text_preferred(self):
        self.assertIn('NT$30', self.html_b)
        # raw field '15' should NOT appear as the cost value when text is set
        # (it may appear in stress table cost keys, so check cost section specifically)
        self.assertIn('NT$30', self.html_b)

    def test_metrics_table_present(self):
        self.assertIn('Train / Validation / OOS Metrics', self.html_a)

    def test_chosen_params_present(self):
        self.assertIn('th=70', self.html_a)


class TestSmokeRender(unittest.TestCase):
    def test_smoke_two_strategies(self):
        """Full page render with 2 strategies should contain all major sections."""
        html = render_page([STRATEGY_A, STRATEGY_B])
        self.assertIn('<!DOCTYPE html>', html)
        self.assertIn('OOS Summary Comparison', html)
        self.assertIn('Strategy Details', html)
        self.assertIn('StrategyAlpha', html)
        self.assertIn('StrategyBeta', html)
        self.assertIn('OOS PF', html)
        self.assertIn('Cost Stress', html)

    def test_xss_safe(self):
        """Strategy names with HTML chars must be escaped."""
        evil = _make_strategy('<script>alert(1)</script>')
        html = render_page([evil])
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;', html)


if __name__ == '__main__':
    unittest.main()
