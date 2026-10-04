"""Active-target concrete no-op vs. a real new increment, with confirmation.

These exercise the real ``Evolution.process`` / ``_compare_stage`` /
``_reconciliation_targets`` / ``_stage_loop`` / ``_resolution_problems`` /
``_confirmation_problems`` paths with synthetic fixtures only: no live vault,
model, credentials or network is touched. A model stub proves machine
behaviour, not semantic quality.

The corrected contract: for ANY authorized target a concrete no-op is legal
only when its content AND state already faithfully represent this source and
scope and no missing increment remains. Covered here:

(A) An active target already fully represented: after the independent
    comparison blocks a duplicate create, the next compile returns an explicit
    per-target no-op resolution and the independent verifier returns an explicit
    ``confirmed``; the same identity/version/body is left unchanged and zero
    records are created.
(B) A real new condition: the next compile reuses the same card_id with an
    update (not a new identity) and the independent verifier confirms it; a
    proposal that misses the required increment (a bare no-op) is rejected by
    the independent verifier and never becomes a done batch.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tongle.engine import Engine
from tongle.evolution import Evolution

try:  # sibling import under ``unittest discover -s tests``
    from fact_review_stub import with_fact_audit
except ImportError:  # pragma: no cover - package-import invocation
    from tests.fact_review_stub import with_fact_audit


class _ActiveNoopHarness(unittest.TestCase):
    TOKEN = '共同令牌'
    MAIN_TEXT = '新增共同令牌材料。'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.e = Engine(self.root / 'state')
        self.e.init(self.root / 'vault'); self.addCleanup(self.e.store.close)
        self.write_config()
        self.ev = Evolution(self.e)

    def write_config(self, **overrides):
        cfg = {'knowledge_mode': 'evolving', 'model_command': ['stub'],
               'knowledge_quiet_seconds': 0,
               # Synthetic-only, generous finite daily allowance so the bounded
               # comparison is limited by the attempt/dispatch ceilings, never
               # by an accidental token wall. Production caps are untouched.
               'knowledge_daily_token_budget': 100000000,
               'knowledge_daily_call_budget': 100000}
        cfg.update(overrides)
        (self.e.home / 'config.json').write_text(json.dumps(cfg))

    def seed(self, key, body, **kw):
        return self.ev.cards.seed('project', key, 'memory', kw.pop('topic', '主题'),
                                  key, body, **kw)

    def capture(self, text=None, event='e', role='user', session='session'):
        rec = self.e.store.capture('project', session, role, event, text or self.MAIN_TEXT)
        self.ev.captured(rec['source_id'], role, session)
        return rec

    def source(self, rec):
        return self.e.store.source(rec['source_id'])

    def run_model(self, model, force=True):
        # The focused fact-audit dispatch is answered structurally by the
        # test-only wrapper so ``model`` sees only the legacy call sequence.
        with patch('tongle.worker.invoke_json', side_effect=with_fact_audit(model)):
            return self.ev.process(force=force)

    def work(self, state=None):
        sql = 'SELECT * FROM knowledge_work'; args = ()
        if state:
            sql += ' WHERE state=?'; args = (state,)
        return [dict(r) for r in self.e.db.execute(sql, args).fetchall()]

    def unblock(self):
        with self.e.transaction():
            self.e.db.execute('UPDATE knowledge_work SET next_at=0')

    def create_op(self, source):
        return {'op': 'create', 'key': self.TOKEN, 'kind': 'memory', 'topic': '主题',
                'title': self.TOKEN, 'body': self.TOKEN + '的新说明。',
                'evidence': [{'source_id': source['source_id'],
                              'version': source['version'], 'quote': source['text']}],
                'reason': '新增材料'}

    def comparison(self, relation):
        """Independent comparison that blocks with one concrete relation."""
        def compare(data):
            cand = {c['card_id']: c for c in data['page']['candidates']}
            out = []
            for p in data['page']['pairs']:
                body = cand[p['card_id']]['body']
                out.append({'op': p['op'], 'card_id': p['card_id'], 'relation': relation,
                            'quote': body[:60],
                            'reason': 'the proposed change overlaps this existing card claim'})
            return {'pairs': out}
        return compare

    def completion_record(self, work_id):
        row = self.e.db.execute(
            "SELECT error FROM knowledge_failures WHERE work_id=? AND state='completion'",
            (work_id,)).fetchone()
        return json.loads(row['error']) if row else None

    @staticmethod
    def payload_card(data, alias):
        """The complete old-card row a payload supplied under one alias.

        The provider only ever sees short identity aliases (cardNNN), so the
        real target identity must be observed by mapping that alias back to the
        whole existing-card row carried in the SAME payload. The row's title and
        body then prove which stored card the obligation names, instead of
        comparing the model-visible alias with the raw stored id.
        """
        existing = data.get('existing') if isinstance(data, dict) else None
        if not isinstance(existing, dict):
            return None
        rows = list(existing.get('cards') or []) + list(existing.get('catalog') or [])
        for entry in rows:
            if isinstance(entry, dict) and entry.get('id') == alias:
                return entry
        return None


class ActiveTargetNoopTests(_ActiveNoopHarness):
    def test_fully_represented_active_target_is_confirmed_noop_zero_changes(self):
        card = self.seed('共同令牌已有卡', self.TOKEN + '已完整承载该主张。')  # active
        rec = self.capture(); source = self.source(rec)
        compare = self.comparison('duplicate')
        verify_targets = []
        verify_identity = []
        noop_alias = []
        noop_identity = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return compare(data), {}
            if 'proposal' in data:                      # independent verify
                recon = data.get('reconciliation') or []
                verify_targets.append(recon)
                verify_identity.append(
                    [(r['card_id'], self.payload_card(data, r['card_id'])) for r in recon])
                return {'approved': True, 'findings': [],
                        'reconciliation_confirmations': [
                            {'card_id': r['card_id'], 'decision': 'confirmed',
                             'reason': '确认该活跃目标已忠实承载本来源与范围，无缺失增量'}
                            for r in recon]}, {}
            recon = data.get('reconciliation')          # compile
            if recon:
                alias = recon[0]['card_id']
                noop_alias.append(alias)
                noop_identity.append(self.payload_card(data, alias))
                return {'operations': [],
                        'reconciliation_resolutions': [
                            {'card_id': alias, 'decision': 'noop',
                             'reason': '同一活跃卡已忠实承载该主张，本来源无缺失增量'}],
                        'reason': '同一活跃卡已忠实承载该主张，本来源无缺失增量'}, {}
            return {'operations': [self.create_op(source)]}, {}

        first = self.run_model(model)
        self.assertEqual(first['processed'], 0)
        row = self.work('retry')[0]
        self.unblock()
        second = self.run_model(model)
        self.assertEqual(second['processed'], 1)
        self.assertEqual(second['failed'], 0)

        # The comparison produced exactly one target; the compile resolution and
        # the independent confirmation name that same real card. Since the model
        # only sees aliases, each alias is resolved through the complete
        # old-card row carried in the SAME payload, and the resolved title/body
        # must equal this specific seed: that proves the alias identity rather
        # than trusting the alias token or a bare nonempty assertion.
        self.assertTrue(noop_alias)
        self.assertEqual(len(set(noop_alias)), 1)
        self.assertTrue(verify_targets)
        self.assertEqual(len(verify_targets[-1]), 1)
        self.assertTrue(noop_identity)
        for observed in noop_identity:
            self.assertIsNotNone(observed)
            self.assertEqual(observed['title'], card['title'])
            self.assertEqual(observed['body'], card['body'])
        self.assertEqual(len(verify_identity[-1]), 1)
        verify_card = verify_identity[-1][0][1]
        self.assertIsNotNone(verify_card)
        self.assertEqual(verify_card['title'], card['title'])
        self.assertEqual(verify_card['body'], card['body'])

        # Same identity, version and body; zero records created.
        after = self.ev.cards.get(card['id'])
        self.assertEqual(after['version'], card['version'])
        self.assertEqual(after['body'], card['body'])
        self.assertEqual([c['id'] for c in self.ev.cards.list('project', include_inactive=True)],
                         [card['id']])

        # The compact completion record ties the confirmed no-op to the target
        # and states the real knowledge change count (zero).
        record = self.completion_record(row['id'])
        self.assertEqual(record['reconciliation']['count'], 1)
        self.assertEqual(record['reconciliation']['knowledge_changes'], 0)
        self.assertEqual(record['reconciliation']['targets'][0]['card_id'], card['id'])


class RealIncrementTests(_ActiveNoopHarness):
    def test_real_new_condition_updates_same_identity_and_confirms(self):
        card = self.seed('共同令牌既有卡', self.TOKEN + '旧条件。')  # active
        rec = self.capture('新增共同令牌材料，并补充一条新的适用条件。')
        source = self.source(rec)
        compare = self.comparison('extends')
        updated_bodies = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return compare(data), {}
            if 'proposal' in data:
                recon = data.get('reconciliation') or []
                return {'approved': True, 'findings': [],
                        'reconciliation_confirmations': [
                            {'card_id': r['card_id'], 'decision': 'confirmed',
                             'reason': '确认同一既有身份已被更新以纳入本来源的新条件'}
                            for r in recon]}, {}
            recon = data.get('reconciliation')
            if recon:
                alias = recon[0]['card_id']
                body = self.TOKEN + '旧条件，并补充本来源的新适用条件。'
                updated_bodies.append(body)
                return {'operations': [
                            {'op': 'update', 'id': alias, 'expected_version': card['version'],
                             'body': body,
                             'evidence': [{'source_id': source['source_id'],
                                           'version': source['version'],
                                           'quote': source['text']}],
                             'reason': '复用既有身份，更新以纳入本来源的新条件'}],
                        'reconciliation_resolutions': [
                            {'card_id': alias, 'decision': 'update',
                             'reason': '复用既有身份，更新以纳入本来源的新条件'}]}, {}
            return {'operations': [self.create_op(source)]}, {}

        first = self.run_model(model)
        self.assertEqual(first['processed'], 0)
        row = self.work('retry')[0]
        self.unblock()
        second = self.run_model(model)
        self.assertEqual(second['processed'], 1)
        self.assertEqual(second['failed'], 0)

        # The SAME identity was updated in place; no new identity was created.
        after = self.ev.cards.get(card['id'])
        self.assertEqual(after['version'], card['version'] + 1)
        self.assertEqual(after['body'], updated_bodies[-1])
        self.assertEqual([c['id'] for c in self.ev.cards.list('project', include_inactive=True)],
                         [card['id']])
        record = self.completion_record(row['id'])
        self.assertEqual(record['reconciliation']['knowledge_changes'], 1)
        # Persisted successful reconciliation evidence: the changed target is
        # the same stored identity, not a fresh one.
        self.assertEqual(record['reconciliation']['count'], 1)
        self.assertEqual(record['reconciliation']['targets'][0]['card_id'], card['id'])

    def test_missing_increment_noop_is_rejected_by_the_verifier(self):
        card = self.seed('共同令牌既有卡', self.TOKEN + '旧条件。')  # active
        rec = self.capture('新增共同令牌材料，并补充一条新的适用条件。')
        source = self.source(rec)
        compare = self.comparison('extends')
        verify_targets = []
        verify_identity = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return compare(data), {}
            if 'proposal' in data:
                recon = data.get('reconciliation') or []
                verify_targets.append(recon)
                verify_identity.append(
                    [(r['card_id'], self.payload_card(data, r['card_id'])) for r in recon])
                # The source truly carries a new condition, so a bare no-op is
                # wrong: the independent verifier must send it back.
                return {'approved': True, 'findings': [],
                        'reconciliation_confirmations': [
                            {'card_id': r['card_id'], 'decision': 'needs_revision',
                             'reason': '本来源确含新增条件，纯 no-op 未处置缺失增量'}
                            for r in recon]}, {}
            recon = data.get('reconciliation')
            if recon:
                alias = recon[0]['card_id']
                return {'operations': [],
                        'reconciliation_resolutions': [
                            {'card_id': alias, 'decision': 'noop',
                             'reason': '声称该既有卡已完整承载，无缺失增量'}],
                        'reason': '声称该既有卡已完整承载，无缺失增量'}, {}
            return {'operations': [self.create_op(source)]}, {}

        first = self.run_model(model)
        self.assertEqual(first['processed'], 0)
        row = self.work('retry')[0]
        self.unblock()
        second = self.run_model(model)
        # The missing-increment no-op was rejected, never published.
        self.assertEqual(second['processed'], 0)
        self.assertEqual(second['failed'], 1)
        self.assertTrue(verify_targets)
        self.assertEqual(len(verify_targets[-1]), 1)
        _verify_alias, verify_card = verify_identity[-1][0]
        self.assertIsNotNone(verify_card)
        self.assertEqual(verify_card['title'], card['title'])
        self.assertEqual(verify_card['body'], card['body'])
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())
        # Bounded: the exhausted attempt is an explicit failure, not a done batch.
        self.unblock()
        third = self.run_model(model)
        self.assertEqual(third['processed'], 0)
        self.assertTrue(self.work('failed'))
        self.assertEqual(self.ev.cards.get(card['id'])['version'], card['version'])


if __name__ == '__main__':
    unittest.main()
