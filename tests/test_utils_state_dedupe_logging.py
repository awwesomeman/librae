"""Unit tests for utils_state, utils_dedupe, and utils_logging."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from utils_state import load_json_state, save_json_state
from utils_dedupe import build_signal_key, is_duplicate
from utils_logging import rotate_if_oversize, append_jsonl


class TestUtilsState(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'state.json')
            data = {'key': 'value', 'num': 42}
            save_json_state(path, data)
            loaded = load_json_state(path)
            self.assertEqual(loaded, data)

    def test_load_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'missing.json')
            self.assertEqual(load_json_state(path), {})

    def test_save_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'sub', 'dir', 'state.json')
            save_json_state(path, {'x': 1})
            self.assertTrue(os.path.exists(path))

    def test_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'state.json')
            save_json_state(path, {'a': 1})
            save_json_state(path, {'b': 2})
            self.assertEqual(load_json_state(path), {'b': 2})


class TestUtilsDedupe(unittest.TestCase):
    def test_build_signal_key(self):
        key = build_signal_key('2026-01-01T00:00:00', '2026-01-01T01:00:00')
        self.assertEqual(key, '2026-01-01T00:00:00::2026-01-01T01:00:00')

    def test_is_duplicate_same(self):
        key = build_signal_key('2026-01-01T00:00:00', '2026-01-01T01:00:00')
        self.assertTrue(is_duplicate(key, key))

    def test_is_duplicate_different(self):
        key1 = build_signal_key('2026-01-01T00:00:00', '2026-01-01T01:00:00')
        key2 = build_signal_key('2026-01-01T00:00:00', '2026-01-01T02:00:00')
        self.assertFalse(is_duplicate(key1, key2))

    def test_is_duplicate_none_last(self):
        key = build_signal_key('2026-01-01T00:00:00', '2026-01-01T01:00:00')
        self.assertFalse(is_duplicate(None, key))


class TestUtilsLogging(unittest.TestCase):
    def test_append_jsonl_writes_line(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'log.jsonl')
            append_jsonl(path, {'a': 1})
            with open(path, encoding='utf-8') as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0]), {'a': 1})

    def test_append_jsonl_accumulates(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'log.jsonl')
            append_jsonl(path, {'n': 1})
            append_jsonl(path, {'n': 2})
            with open(path, encoding='utf-8') as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[1])['n'], 2)

    def test_append_jsonl_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'sub', 'log.jsonl')
            append_jsonl(path, {'x': True})
            self.assertTrue(os.path.exists(path))

    def test_rotate_if_oversize_small_file_not_rotated(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'log.jsonl')
            append_jsonl(path, {'data': 'small'})
            rotate_if_oversize(path, max_mb=10)
            self.assertTrue(os.path.exists(path))
            self.assertFalse(os.path.exists(path + '.1'))

    def test_rotate_if_oversize_large_file_rotated(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'log.jsonl')
            # write a file that exceeds 0.00001 MB threshold
            with open(path, 'w', encoding='utf-8') as f:
                f.write('x' * 200)
            rotate_if_oversize(path, max_mb=0.0001)
            self.assertFalse(os.path.exists(path))
            self.assertTrue(os.path.exists(path + '.1'))

    def test_rotate_if_oversize_missing_file_no_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'nonexistent.jsonl')
            rotate_if_oversize(path, max_mb=10)  # must not raise


if __name__ == '__main__':
    unittest.main()
