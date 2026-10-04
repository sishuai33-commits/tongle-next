"""Backlog accounting: one-time UTC-day budget credit and ledger timestamps.

These tests read only local sources. They never touch a production Vault,
credentials, the network, or the SDK.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from tongle.engine import Engine
from tongle.evolution import Evolution, config, _nonneg_int


def _today():
    return time.strftime('%Y-%m-%d', time.gmtime())


class ConfigCreditTests(unittest.TestCase):
    """knowledge_oneoff_budget_credit is additive, daily and read-only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.e = Engine(self.root / 'state')
        self.e.init(self.root / 'vault'); self.addCleanup(self.e.store.close)
        self.cfg_path = self.e.home / 'config.json'

    def write(self, settings):
        self.cfg_path.write_text(json.dumps(settings))

    def test_credit_is_added_on_the_current_utc_day(self):
        self.write({'knowledge_mode': 'evolving',
                    'knowledge_daily_token_budget': 6000000,
                    'knowledge_daily_call_budget': 180,
                    'knowledge_oneoff_budget_credit':
                        {'day': _today(), 'tokens': 300000, 'calls': 40}})
        got = config(self.e)
        self.assertEqual(got['knowledge_daily_token_budget'], 6300000)
        self.assertEqual(got['knowledge_daily_call_budget'], 220)

    def test_credit_defaults_add_to_unspecified_base(self):
        self.write({'knowledge_oneoff_budget_credit':
                        {'day': _today(), 'tokens': 5, 'calls': 7}})
        got = config(self.e)
        self.assertEqual(got['knowledge_daily_token_budget'], 300000 + 5)
        self.assertEqual(got['knowledge_daily_call_budget'], 40 + 7)

    def test_credit_does_not_touch_disk_file(self):
        original = {'knowledge_daily_token_budget': 6000000,
                    'knowledge_oneoff_budget_credit':
                        {'day': _today(), 'tokens': 1000, 'calls': 3}}
        self.write(original)
        config(self.e)
        self.assertEqual(json.loads(self.cfg_path.read_text()), original)
        # The base map in memory is not mutated either.
        base = {'knowledge_oneoff_budget_credit':
                    {'day': _today(), 'tokens': 1, 'calls': 1}}
        self.write(base)
        before = dict(base)
        config(self.e)
        self.assertEqual(base, before)

    def test_credit_expires_when_day_does_not_match(self):
        self.write({'knowledge_daily_token_budget': 6000000,
                    'knowledge_daily_call_budget': 180,
                    'knowledge_oneoff_budget_credit':
                        {'day': '1999-01-01', 'tokens': 300000, 'calls': 40}})
        got = config(self.e)
        self.assertEqual(got['knowledge_daily_token_budget'], 6000000)
        self.assertEqual(got['knowledge_daily_call_budget'], 180)

    def test_malformed_credit_is_ignored_entirely(self):
        bad = [
            {'day': _today(), 'tokens': -1, 'calls': 40},
            {'day': _today(), 'tokens': 300000, 'calls': -40},
            {'day': _today(), 'tokens': True, 'calls': 40},
            {'day': _today(), 'tokens': 300000, 'calls': False},
            {'day': _today(), 'tokens': '300000', 'calls': 40},
            {'day': _today(), 'tokens': 1.5, 'calls': 40},
            {'day': _today(), 'tokens': 300000, 'calls': None},
            {'day': _today(), 'tokens': 300000},
            {'tokens': 300000, 'calls': 40},
            [{'day': _today(), 'tokens': 300000, 'calls': 40}],
            'credit',
        ]
        for credit in bad:
            with self.subTest(credit=credit):
                self.write({'knowledge_daily_token_budget': 6000000,
                            'knowledge_daily_call_budget': 180,
                            'knowledge_oneoff_budget_credit': credit})
                got = config(self.e)
                self.assertEqual(got['knowledge_daily_token_budget'], 6000000)
                self.assertEqual(got['knowledge_daily_call_budget'], 180)

    def test_absent_credit_leaves_base_budgets(self):
        self.write({'knowledge_daily_token_budget': 6000000,
                    'knowledge_daily_call_budget': 180})
        got = config(self.e)
        self.assertEqual(got['knowledge_daily_token_budget'], 6000000)
        self.assertEqual(got['knowledge_daily_call_budget'], 180)

    def test_nonneg_int_rejects_bool(self):
        self.assertTrue(_nonneg_int(0))
        self.assertTrue(_nonneg_int(3))
        self.assertFalse(_nonneg_int(True))
        self.assertFalse(_nonneg_int(False))
        self.assertFalse(_nonneg_int(-1))
        self.assertFalse(_nonneg_int('3'))
        self.assertFalse(_nonneg_int(None))


class LedgerTimestampTests(unittest.TestCase):
    """_inputs preserves part content and attaches real ledger timestamps."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.e = Engine(self.root / 'state')
        self.e.init(self.root / 'vault'); self.addCleanup(self.e.store.close)
        self.ev = Evolution(self.e)

    def _job(self, sid, version):
        source = self.e.store.source(sid)
        chosen = list(self.ev._source_parts(source))
        return {'inputs': json.dumps(chosen)}, source

    def test_inputs_preserves_text_and_adds_real_ledger_fields(self):
        text = '第一条决定：暂缓实施方案，等待复核。'
        receipt = self.e.store.capture('project', 'session', 'user', 'one', text)
        job, source = self._job(receipt['source_id'], receipt['version'])
        got = self.ev._inputs(job)
        self.assertTrue(got)
        for part in got:
            self.assertEqual(part['text'], text)
            self.assertEqual(part['role'], source['role'])
            self.assertEqual(part['source_id'], source['source_id'])
            self.assertEqual(part['version'], source['current_version'])
            self.assertEqual(part['ledger_captured_at'], source['created_at'])
            self.assertEqual(part['ledger_version_at'], source['version_created_at'])
        # Timestamps come from the ledger, not from the wall clock.
        self.assertNotEqual(got[0]['ledger_captured_at'], None)

    def test_inputs_keeps_original_part_unchanged(self):
        receipt = self.e.store.capture('project', 'session', 'user', 'one', '正文')
        source = self.e.store.source(receipt['source_id'])
        original = list(self.ev._source_parts(source))
        job = {'inputs': json.dumps(original)}
        self.ev._inputs(job)
        # The stored job inputs are untouched; enrichment is a copy.
        for before, after in zip(original, json.loads(job['inputs'])):
            self.assertEqual(before, after)
            self.assertNotIn('ledger_captured_at', after)
            self.assertNotIn('ledger_version_at', after)

    def test_superseded_input_is_rejected(self):
        path = self.root / 'source.md'
        path.write_text('旧文本')
        receipt = self.e.store.ingest('project', str(path))
        job, _ = self._job(receipt['source_id'], receipt['version'])
        path.write_text('新文本')
        self.e.store.ingest('project', str(path))
        with self.assertRaises(ValueError):
            self.ev._inputs(job)


if __name__ == '__main__':
    unittest.main()
