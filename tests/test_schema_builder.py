#!/usr/bin/env python3
"""Tests for scripts.reporting.schema_builder."""

import os
import sys
import json
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from quant_lab.backtest import Periods
from scripts.reporting.schema_builder import (
    build_sample_periods,
    build_cost_settings,
    build_strategy_output,
    validate_against_report_schema,
)

FAKE_PERIODS = Periods('2024-01-01', '2025-03-31', '2025-04-01', '2025-06-30', '2025-07-01', '2026-03-03')

FAKE_METRICS = {
    'trades': 42, 'win_rate': 0.55, 'avg_ret': 0.003,
    'pf': 1.8, 'equity': 1.12, 'ann_return': 0.15,
    'ann_sharpe': 1.2, 'ann_vol': 0.12, 'mdd': 0.08,
}

FAKE_STRICT = {
    'protocol': 'strict: train-select, validation-check, oos-once',
    'chosen_params': {'th': 70, 'bn': 3, 'en': 10},
    'train': dict(FAKE_METRICS),
    'validation': dict(FAKE_METRICS),
    'validation_cost_stress': {'2.0': dict(FAKE_METRICS)},
    'oos_final_once': dict(FAKE_METRICS),
}


class TestBuildSamplePeriods(unittest.TestCase):
    def test_structure(self):
        sp = build_sample_periods(FAKE_PERIODS)
        self.assertEqual(sp['train']['start'], '2024-01-01')
        self.assertEqual(sp['oos']['end'], '2026-03-03')
        self.assertIn('validation', sp)


class TestBuildCostSettings(unittest.TestCase):
    def test_keys(self):
        cs = build_cost_settings(fee=100, slippage=0, tax=0, round_trip_cost=2.0, unit='points')
        self.assertEqual(cs, {
            'unit': 'points',
            'fee': 100,
            'slippage': 0,
            'tax': 0,
            'round_trip_cost': 2.0,
            'round_trip_cost_text': 'round_trip_cost=2.0 points (fee=100, slippage=0, tax=0)',
        })

    def test_cost_text_format(self):
        cs = build_cost_settings(fee=4, slippage=4, tax=0, round_trip_cost=8, unit='bps')
        self.assertEqual(cs['round_trip_cost_text'], 'round_trip_cost=8 bps (fee=4, slippage=4, tax=0)')
        self.assertNotIn('notes', cs)


class TestBuildStrategyOutput(unittest.TestCase):
    def _build(self, **kwargs):
        defaults = dict(
            strategy='Test_v1.0',
            instrument='MXFR1',
            data='Shioaji',
            periods=FAKE_PERIODS,
            cost_settings={'unit': 'points', 'fee': 100, 'slippage': 0, 'tax': 0, 'round_trip_cost': 2.0, 'round_trip_cost_text': 'round_trip_cost=2.0 points (fee=100, slippage=0, tax=0)'},
            strict_result=FAKE_STRICT,
        )
        defaults.update(kwargs)
        return build_strategy_output(**defaults)

    def test_key_order(self):
        out = self._build()
        keys = list(out.keys())
        expected_prefix = [
            'strategy', 'instrument', 'data', 'sample_periods', 'cost_settings',
            'protocol', 'chosen_params', 'train', 'validation',
            'validation_cost_stress', 'oos_final_once',
        ]
        self.assertEqual(keys[:len(expected_prefix)], expected_prefix)

    def test_contains_sample_periods(self):
        out = self._build()
        self.assertIn('sample_periods', out)
        self.assertIn('train', out['sample_periods'])

    def test_contains_cost_settings(self):
        out = self._build()
        self.assertIn('cost_settings', out)
        self.assertEqual(out['cost_settings']['fee'], 100)

    def test_extras_appended(self):
        out = self._build(extras={'custom_field': 42})
        self.assertEqual(out['custom_field'], 42)
        keys = list(out.keys())
        self.assertEqual(keys[-1], 'custom_field')

    def test_strict_result_passthrough(self):
        extra_strict = dict(FAKE_STRICT, extra_key='hello')
        out = self._build(strict_result=extra_strict)
        self.assertEqual(out['extra_key'], 'hello')


class TestValidateAgainstReportSchema(unittest.TestCase):
    def _make_schema(self, brief=None, full=None):
        schema = {}
        if brief is not None:
            schema['brief_required'] = brief
        if full is not None:
            schema['full_required'] = full
        fd, path = tempfile.mkstemp(suffix='.json')
        with os.fdopen(fd, 'w') as f:
            json.dump(schema, f)
        return path

    def test_valid_passes(self):
        out = build_strategy_output(
            strategy='T', instrument='I', data='D',
            periods=FAKE_PERIODS,
            cost_settings={'unit': 'points', 'fee': 0, 'slippage': 0, 'tax': 0, 'round_trip_cost': 0, 'round_trip_cost_text': 'round_trip_cost=0 points (fee=0, slippage=0, tax=0)'},
            strict_result=FAKE_STRICT,
        )
        schema_path = self._make_schema(
            brief=['strategy', 'train.trades', 'oos_final_once.mdd'],
            full=['strategy', 'protocol', 'train'],
        )
        try:
            validate_against_report_schema(out, schema_path=schema_path)
        finally:
            os.unlink(schema_path)

    def test_missing_raises(self):
        out = {'strategy': 'T'}
        schema_path = self._make_schema(
            brief=['strategy', 'train.trades'],
            full=['strategy', 'nonexistent_key'],
        )
        try:
            with self.assertRaises(ValueError) as ctx:
                validate_against_report_schema(out, schema_path=schema_path)
            msg = str(ctx.exception)
            self.assertIn('train.trades', msg)
            self.assertIn('nonexistent_key', msg)
        finally:
            os.unlink(schema_path)

    def test_missing_deep_path(self):
        out = {'strategy': 'T', 'train': {'trades': 5}}
        schema_path = self._make_schema(brief=['train.win_rate'])
        try:
            with self.assertRaises(ValueError) as ctx:
                validate_against_report_schema(out, schema_path=schema_path)
            self.assertIn('train.win_rate', str(ctx.exception))
        finally:
            os.unlink(schema_path)

    @unittest.skip("templates/report_schema.json removed")
    def test_real_schema(self):
        """Validate a full output against the actual report_schema.json."""
        pass


if __name__ == '__main__':
    unittest.main()
