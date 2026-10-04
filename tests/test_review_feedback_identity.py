"""Review-feedback identity: a rejected review's finding must stay bound to the
SAME storage identity across a later compile call, even though the transport
re-assigns short aliases (``card002``/``src001``) per call.

These exercise the real ``Evolution.process`` / ``_stage_loop`` / source-ledger
paths with synthetic fixtures only. No live vault, model, credentials or
network is touched. Where a model fixture is used it proves machine behaviour,
not semantic quality.

The defects these pin down:

* ``_bounded_findings`` used to collapse whitespace and the historical alias map
  was a ``{oldAlias: rawId}`` dict whose *values* the transport never
  re-encoded (``model_refs`` only encodes typed keys). The repair keeps the
  finding bytes exact, records each mentioned alias as a bounded typed list
  entry ``{historical_alias, card_id|source_id}`` so the ordinary transport
  re-encodes the stable id to the CURRENT call's alias, and leaves an
  unknown/untyped alias explicitly unresolved rather than guessed.
* a missing/legacy envelope used to resend old freeform text unfiltered. The
  repair distinguishes host-built structural findings (safe to replay) from
  historical review prose, which is withheld entirely behind a quote-free
  refresh notice unless an authentic envelope bound to the exact rejecting
  verify dispatch (captured from its own payload and reverse alias map BEFORE
  the provider call) still validates.
* the feedback was validated once before ``_stage_loop``; it is now revalidated
  immediately before EVERY compile dispatch against the LIVE source ledger
  (version, full text, availability) and the referenced card versions/hashes.
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tongle.engine import Engine
from tongle.evolution import (Evolution, FEEDBACK_ENVELOPE_FORMAT,
                              FEEDBACK_ORIGIN_REVIEW, FEEDBACK_ORIGIN_HOST,
                              MAX_FEEDBACK_REFS, STALE_FEEDBACK_NOTICE)
from tongle.comparison_review import RECONCILIATION_STORE_FORMAT


def _alias_for(data, key):
    """The transport alias of the catalog/card row carrying ``key``."""
    existing = data.get('existing') or {}
    for row in list(existing.get('cards') or []) + list(existing.get('catalog') or []):
        if row.get('key') == key and isinstance(row.get('id'), str):
            return row['id']
    raise AssertionError('no alias for key ' + repr(key))


def _source_alias(data):
    """The transport alias of the first supplied source id."""
    for row in data.get('sources') or []:
        sid = row.get('source_id')
        if isinstance(sid, str) and sid:
            return sid
    raise AssertionError('no source alias in payload')


class _Harness(unittest.TestCase):
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
               # Synthetic only: generous finite allowance so the bounded flow
               # is limited by the attempt/stage ceilings, never a token wall.
               'knowledge_daily_token_budget': 100000000,
               'knowledge_daily_call_budget': 100000}
        cfg.update(overrides)
        (self.e.home / 'config.json').write_text(json.dumps(cfg))

    def seed(self, key, body, **kw):
        return self.ev.cards.seed('project', key, 'memory', kw.pop('topic', '主题'),
                                  key, body, **kw)

    def capture(self, text, event='one', role='user', session='session'):
        rec = self.e.store.capture('project', session, role, event, text)
        self.ev.captured(rec['source_id'], role, session)
        return rec

    def source(self, rec):
        return self.e.store.source(rec['source_id'])

    def run_model(self, model, force=True):
        with patch('tongle.worker.invoke_json', side_effect=model):
            return self.ev.process(force=force)

    def work(self, state=None):
        sql = 'SELECT * FROM knowledge_work'; args = ()
        if state:
            sql += ' WHERE state=?'; args = (state,)
        return [dict(r) for r in self.e.db.execute(sql, args).fetchall()]

    def unblock(self):
        with self.e.transaction():
            self.e.db.execute('UPDATE knowledge_work SET next_at=0')

    def bump_source(self, sid, extra):
        """Add a new immutable version of an existing source in the ledger."""
        src = self.e.store.source(sid)
        new_text = src['text'] + extra
        original = new_text.encode('utf-8')
        with self.e.transaction():
            self.e.db.execute(
                'INSERT INTO versions(source_id,version,text,original,hash,created_at)'
                ' VALUES(?,?,?,?,?,?)',
                (sid, src['current_version'] + 1, new_text, original,
                 hashlib.sha256(original).hexdigest(), '0'))
            self.e.db.execute('UPDATE sources SET current_version=? WHERE id=?',
                              (src['current_version'] + 1, sid))
        return src['current_version'] + 1

    def stored_bundle(self, work_id):
        row = self.e.db.execute('SELECT bundle FROM knowledge_work WHERE id=?',
                                (work_id,)).fetchone()
        return json.loads(row['bundle'])


class BoundedFindingsUnitTests(_Harness):
    def test_limits_are_honest_and_duplicates_collapse(self):
        many = [{'issue': 'issue-%d' % i} for i in range(200)]
        huge = [{'issue': 'x' * 100000}]
        self.assertEqual(len(self.ev._bounded_findings(many)), 8)
        self.assertEqual(len(self.ev._bounded_findings(huge)[0]['finding']), 1600)
        # The same finding is never sent twice.
        dup = [{'finding': 'same defect'}, {'finding': 'same defect'},
               {'finding': 'other defect'}]
        self.assertEqual([e['finding'] for e in self.ev._bounded_findings(dup)],
                         ['same defect', 'other defect'])

    def test_structured_refs_and_minimal_correction_survive(self):
        real = 'a' * 32
        src = 'b' * 32
        kept = self.ev._bounded_findings([{
            'finding': 'structured defect', 'card_id': real, 'source_id': src,
            'minimal_correction': 'reduce the scope to the current round'}])
        self.assertEqual(kept[0]['card_id'], real)
        self.assertEqual(kept[0]['source_id'], src)
        self.assertIn('reduce the scope', kept[0]['minimal_correction'])
        # A non-identity (non 32-hex) value is never misread as a stable ref.
        other = self.ev._bounded_findings([{'finding': 'x', 'card_id': 'card002'}])
        self.assertNotIn('card_id', other[0])

    def test_exact_whitespace_survives_bounding(self):
        finding = 'line one\n\nline two  with   double spaces'
        correction = 'fix\n  this   exactly'
        kept = self.ev._bounded_findings([{'finding': finding,
                                           'minimal_correction': correction}])
        self.assertEqual(kept[0]['finding'], finding)
        self.assertEqual(kept[0]['minimal_correction'], correction)

    def test_historical_alias_map_is_bounded_and_typed_and_unresolved_stated(self):
        alias_map = {'card%03d' % i: '%032d' % i for i in range(1, 20)}
        text = ' '.join('card%03d' % i for i in range(1, 20))
        entries = self.ev._attach_historical_aliases([{'finding': text}], alias_map)
        self.assertLessEqual(len(entries[0]['historical_aliases']), MAX_FEEDBACK_REFS)
        # Every surviving entry is a typed list item, never a raw dict.
        for ref in entries[0]['historical_aliases']:
            self.assertIn('historical_alias', ref)
            self.assertIn('card_id', ref)
        # An alias that was never in the invocation is never guessed.
        unknown = self.ev._attach_historical_aliases([{'finding': 'card999 broke it'}],
                                                     {'card001': 'a' * 32})
        self.assertNotIn('historical_aliases', unknown[0])
        self.assertEqual(unknown[0]['unresolved_aliases'], ['card999'])
        # ``minimal_correction`` mentions are scanned too.
        corr = self.ev._attach_historical_aliases(
            [{'finding': 'no alias here', 'minimal_correction': 'see card001'}],
            {'card001': 'b' * 32})
        self.assertEqual(corr[0]['historical_aliases'],
                         [{'historical_alias': 'card001', 'card_id': 'b' * 32}])


class ReviewFeedbackProcessTests(_Harness):
    def _rejecting_model(self, finding_for):
        """Compile is a no-op; verify rejects with a finding built per payload.

        ``finding_for(payload)`` returns a list of finding dicts (or strings).
        """
        state = {'compile': []}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:  # verify
                return {'approved': False, 'findings': finding_for(data)}, {}
            state['compile'].append(data)
            return {'operations': [], 'reason': 'synthetic no-op'}, {}

        return model, state

    def test_short_finding_reaches_second_compile_without_duplication(self):
        self.capture('记录需要更新。')
        model, state = self._rejecting_model(lambda data: [{'reason': 'short defect'}])
        first = self.run_model(model)
        self.assertEqual(first['processed'], 0)
        self.assertEqual(self.work('retry')[0]['state'], 'retry')
        state['compile'].clear()
        self.unblock()
        self.run_model(model)
        self.assertTrue(state['compile'])
        second = state['compile'][0]
        # The validated bounded retry feedback is delivered exactly once...
        self.assertIn('short defect', json.dumps(second.get('retry_findings'),
                                                ensure_ascii=False))
        # ...and never duplicated as an unfiltered generic payload field.
        self.assertNotIn('feedback', second)

    def test_exact_text_and_correction_survive_process(self):
        self.capture('记录需要更新。')
        text = 'first line\n\nsecond  line with   spaces'
        correction = 'keep\n  exact  spacing'
        model, state = self._rejecting_model(
            lambda data: [{'finding': text, 'minimal_correction': correction}])
        self.run_model(model)
        state['compile'].clear()
        self.unblock()
        self.run_model(model)
        retry = state['compile'][0]['retry_findings'][0]
        self.assertEqual(retry['finding'], text)
        self.assertEqual(retry['minimal_correction'], correction)

    def test_alias_reorder_still_binds_original_card_and_source_ids(self):
        b = self.seed('乙卡', '乙的正文与记录材料。')
        rec = self.capture('记录需要更新。')
        first = {}

        def finding_for(data):
            calias = _alias_for(data, '乙卡')
            salias = _source_alias(data)
            first['card'] = calias; first['src'] = salias
            return [{'finding': 'alias ' + calias + ' and ' + salias + ' overlap',
                     'minimal_correction': 'narrow the claim to avoid the overlap'}]

        model, state = self._rejecting_model(finding_for)
        self.run_model(model)
        work_id = self.work('retry')[0]['id']
        stored = self.stored_bundle(work_id)
        entry = stored['feedback'][0]
        # The text is kept byte-for-byte; the stable identities are preserved
        # as a typed bounded list beside it.
        self.assertIn(first['card'], entry['finding'])
        self.assertIn(first['src'], entry['finding'])
        self.assertIn({'historical_alias': first['card'], 'card_id': b['id']},
                      entry['historical_aliases'])
        self.assertIn({'historical_alias': first['src'],
                       'source_id': rec['source_id']},
                      entry['historical_aliases'])
        self.assertIn('narrow the claim', entry['minimal_correction'])
        self.assertEqual(stored['feedback_envelope']['format'], FEEDBACK_ENVELOPE_FORMAT)
        self.assertEqual(stored['feedback_envelope']['work_id'], work_id)
        self.assertEqual(stored['feedback_origin'], FEEDBACK_ORIGIN_REVIEW)

        state['compile'].clear()
        self.unblock()
        self.run_model(model)
        second = state['compile'][0]
        retry = second['retry_findings'][0]
        current_card = _alias_for(second, '乙卡')
        current_source = _source_alias(second)
        # The rejected finding carried no structured top-level refs, so the host
        # must not invent ``card_id``/``source_id`` on the entry; the same
        # storage identities live only in the typed historical-alias entries.
        self.assertNotIn('card_id', retry)
        self.assertNotIn('source_id', retry)
        card_refs = [r for r in retry['historical_aliases']
                     if r['historical_alias'] == first['card']]
        src_refs = [r for r in retry['historical_aliases']
                    if r['historical_alias'] == first['src']]
        # Exactly one entry per mentioned alias, so a reordered ``card002`` can
        # never redirect the finding: the transport re-encoded the SAME storage
        # identities to THIS call's aliases.
        self.assertEqual(len(card_refs), 1)
        self.assertEqual(len(src_refs), 1)
        self.assertEqual(card_refs[0]['card_id'], current_card)
        self.assertEqual(src_refs[0]['source_id'], current_source)
        self.assertEqual(retry['minimal_correction'], entry['minimal_correction'])

    def test_changed_card_suppresses_historical_text(self):
        b = self.seed('乙卡', '乙的正文与记录材料。')
        self.capture('记录需要更新。')

        def finding_for(data):
            alias = _alias_for(data, '乙卡')
            return [{'finding': 'alias ' + alias + ' overlaps', 'card_id': alias}]

        model, state = self._rejecting_model(finding_for)
        self.run_model(model)
        # The referenced card moves before the retry: the whole historical
        # finding text must be withheld, not resent against the new body.
        self.ev.cards.manual_edit(b['id'], 1, '乙的正文与记录材料（人工修订）。')
        state['compile'].clear()
        self.unblock()
        self.run_model(model)
        second = state['compile'][0]
        blob = json.dumps(second.get('retry_findings') or [], ensure_ascii=False)
        self.assertNotIn('overlaps', blob)
        self.assertIn(STALE_FEEDBACK_NOTICE[:30], blob)

    def test_source_change_between_compile_requests_withholds_stale_finding(self):
        rec = self.capture('记录需要更新。')
        self.seed('乙卡', '乙的正文与记录材料。')

        def finding_for(data):
            alias = _alias_for(data, '乙卡')
            return [{'finding': 'alias ' + alias + ' overlaps', 'card_id': alias}]

        model, _state = self._rejecting_model(finding_for)
        self.run_model(model)
        self.assertTrue(self.work('retry'))

        state = {'compile': [], 'requests': 0}

        def second_model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            state['compile'].append(data)
            state['requests'] += 1
            if state['requests'] == 1:
                # A new version of a supplied source appears BETWEEN two compile
                # dispatches of the same attempt.
                self.bump_source(rec['source_id'], '\n追加内容。')
                return {'needs_cards': [_alias_for(data, '乙卡')]}, {}
            return {'operations': [], 'reason': 'synthetic no-op'}, {}

        self.unblock()
        self.run_model(second_model)
        self.assertGreaterEqual(len(state['compile']), 2)
        first_blob = json.dumps(state['compile'][0].get('retry_findings') or [],
                                ensure_ascii=False)
        second_blob = json.dumps(state['compile'][1].get('retry_findings') or [],
                                 ensure_ascii=False)
        self.assertIn('overlaps', first_blob)
        self.assertNotIn('overlaps', second_blob)
        self.assertIn(STALE_FEEDBACK_NOTICE[:30], second_blob)

    def test_changed_permission_suppresses_historical_text(self):
        self.seed('乙卡', '乙的正文与记录材料。')
        self.capture('记录需要更新。')

        def finding_for(data):
            alias = _alias_for(data, '乙卡')
            return [{'finding': 'alias ' + alias + ' overlaps', 'card_id': alias}]

        model, state = self._rejecting_model(finding_for)
        self.run_model(model)
        # Narrowing the authorization scope invalidates the old feedback; it is
        # replaced by body-free responsibility findings, never replayed.
        self.write_config(library_excluded_projects=['project'])
        state['compile'].clear()
        self.unblock()
        self.run_model(model)
        second = state['compile'][0]
        blob = json.dumps(second.get('retry_findings') or [], ensure_ascii=False)
        self.assertNotIn('overlaps', blob)

    def test_card_mutation_during_verify_withholds_stale_finding(self):
        b = self.seed('乙卡', '乙的正文与记录材料。')
        self.capture('记录需要更新。')

        def mutating_model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:  # verify: edit the referenced card mid-invoke
                alias = _alias_for(data, '乙卡')
                self.ev.cards.manual_edit(b['id'], 1, '乙的正文（验证期间被改动）。')
                return {'approved': False, 'findings': [
                    {'finding': 'alias ' + alias + ' overlaps', 'card_id': alias}]}, {}
            return {'operations': [], 'reason': 'synthetic no-op'}, {}

        self.run_model(mutating_model)
        work = self.work('retry')
        self.assertTrue(work)
        stored = self.stored_bundle(work[0]['id'])
        self.assertEqual(stored['feedback_origin'], FEEDBACK_ORIGIN_REVIEW)
        # The envelope bound the payload-time (pre-mutation) card version, so
        # the later replay can detect the change.
        self.assertTrue(stored['feedback_envelope']['card_bindings'])

        state = {'compile': []}

        def replay(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            state['compile'].append(data)
            return {'operations': [], 'reason': 'synthetic no-op'}, {}

        self.unblock()
        self.run_model(replay)
        blob = json.dumps(state['compile'][0].get('retry_findings') or [],
                          ensure_ascii=False)
        self.assertNotIn('overlaps', blob)
        self.assertIn(STALE_FEEDBACK_NOTICE[:30], blob)

    def test_card_change_between_compile_requests_withholds_stale_finding(self):
        b = self.seed('乙卡', '乙的正文与记录材料。')
        self.capture('记录需要更新。')

        def finding_for(data):
            alias = _alias_for(data, '乙卡')
            return [{'finding': 'alias ' + alias + ' overlaps', 'card_id': alias}]

        model, _state = self._rejecting_model(finding_for)
        self.run_model(model)
        self.assertTrue(self.work('retry'))

        state = {'compile': [], 'requests': 0}

        def second_model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {'approved': False, 'findings': [{'reason': 'still blocked'}]}, {}
            state['compile'].append(data)
            state['requests'] += 1
            if state['requests'] == 1:
                # Mutate the referenced card BETWEEN two compile dispatches of
                # the same attempt, after a needs_cards request.
                self.ev.cards.manual_edit(b['id'], 1, '乙的正文（编译回合之间改动）。')
                return {'needs_cards': [_alias_for(data, '乙卡')]}, {}
            return {'operations': [], 'reason': 'synthetic no-op'}, {}

        self.unblock()
        self.run_model(second_model)
        self.assertGreaterEqual(len(state['compile']), 2)
        first_blob = json.dumps(state['compile'][0].get('retry_findings') or [],
                                ensure_ascii=False)
        second_blob = json.dumps(state['compile'][1].get('retry_findings') or [],
                                 ensure_ascii=False)
        # The first dispatch of the attempt still carried the finding...
        self.assertIn('overlaps', first_blob)
        # ...but the card moved before the second dispatch, so the old
        # finding/quote must not be resent against the new body.
        self.assertNotIn('overlaps', second_blob)
        self.assertIn(STALE_FEEDBACK_NOTICE[:30], second_blob)

    def test_readonly_source_and_card_bytes_unchanged(self):
        b = self.seed('乙卡', '乙的正文与记录材料。')
        rec = self.capture('记录需要更新。')
        before = self.source(rec)['text']
        before_card = self.ev.cards.get(b['id'])

        def finding_for(data):
            alias = _alias_for(data, '乙卡')
            return [{'finding': 'alias ' + alias + ' overlaps', 'card_id': alias}]

        model, _state = self._rejecting_model(finding_for)
        self.run_model(model)
        self.unblock()
        self.run_model(model)
        # The exact source text and the card body/version are never rewritten by
        # the feedback/alias transport.
        self.assertEqual(self.source(rec)['text'], before)
        after_card = self.ev.cards.get(b['id'])
        self.assertEqual(after_card['body'], before_card['body'])
        self.assertEqual(after_card['version'], before_card['version'])


class EnvelopeReplayUnitTests(_Harness):
    def _source_payload(self):
        rec = self.capture('包裹正文。')
        src = self.source(rec)
        part = next(self.ev._source_parts(src))
        return [{'source_id': part['source_id'], 'version': part['version'],
                 'part': part['part'], 'text': part['text']}]

    def _base(self, entries=None, cards=None, sources=None):
        if sources is None:
            sources = self._source_payload()
        if entries is None:
            entries = [{'finding': 'historical finding card001 on real id',
                        'card_id': 'a' * 32}]
        payload = {'existing': {'cards': cards or [], 'catalog': []},
                   'sources': sources}
        job = {'id': 'w1', 'project': 'project'}
        context = self.ev._verify_invocation_context(job, 'c1', payload, {})
        envelope = self.ev._review_feedback_envelope(context, entries)
        return {'feedback': entries, 'feedback_origin': FEEDBACK_ORIGIN_REVIEW,
                'feedback_envelope': envelope,
                'sources': sources,
                'source_snapshot': {p['source_id']: p['version'] for p in sources},
                'scope_fingerprint': context['scope_fingerprint'],
                'contract': context['contract']}

    def test_current_envelope_replays_the_same_finding(self):
        base = self._base()
        out = self.ev._replay_feedback(base, {'id': 'w1', 'project': 'project'})
        self.assertEqual(out, base['feedback'])

    def test_host_structural_feedback_replays_without_envelope(self):
        # Host-built validation feedback carries no model prose and no envelope;
        # it must still reach the next compile call.
        host = {'feedback': [{'finding': 'operation[0] field=body: empty body'}],
                'feedback_origin': FEEDBACK_ORIGIN_HOST, 'sources': []}
        out = self.ev._replay_feedback(host, {'id': 'w1', 'project': 'project'})
        self.assertEqual(out, host['feedback'])

    def test_legacy_bundle_without_envelope_withholds_text(self):
        # A stale mutable alias map must never attach identities to cached
        # feedback that has no bound envelope; the unknown old text is withheld.
        self.ev._last_reference_map = {'card001': 'a' * 32}
        base = {'feedback': [{'finding': 'cached card001 issue'}], 'sources': []}
        job = {'id': 'w1', 'project': 'project'}
        self.assertEqual(self.ev._replay_feedback(base, job),
                         [{'finding': STALE_FEEDBACK_NOTICE}])
        # A missing origin is unknown even when no envelope is present.
        unknown = {'feedback': [{'finding': 'legacy review prose'}]}
        self.assertEqual(self.ev._replay_feedback(unknown, job),
                         [{'finding': STALE_FEEDBACK_NOTICE}])

    def test_stale_envelope_withholds_text(self):
        base = self._base()
        base['feedback_envelope']['scope_fingerprint'] = 'drifted'
        out = self.ev._replay_feedback(base, {'id': 'w1', 'project': 'project'})
        self.assertEqual(out, [{'finding': STALE_FEEDBACK_NOTICE}])

        # A work mismatch is also stale (never bound to a different batch).
        base = self._base()
        out = self.ev._replay_feedback(base, {'id': 'other', 'project': 'project'})
        self.assertEqual(out, [{'finding': STALE_FEEDBACK_NOTICE}])

        # A changed referenced card version/hash is stale. The binding is taken
        # from the payload the verifier actually saw, not the live store.
        card = self.seed('目标卡', '目标正文。')
        sources = self._source_payload()
        base = self._base(entries=[{'finding': 'x', 'card_id': card['id']}],
                          cards=[card], sources=sources)
        self.ev.cards.manual_edit(card['id'], 1, '目标正文已改。')
        out = self.ev._replay_feedback(base, {'id': 'w1', 'project': 'project'})
        self.assertEqual(out, [{'finding': STALE_FEEDBACK_NOTICE}])

    def test_source_ledger_change_is_detected_even_with_matching_snapshot(self):
        sources = self._source_payload()
        base = self._base(sources=sources)
        # The stored snapshot object is unchanged, but the live ledger version
        # of a supplied source moved: the replay must still be withheld.
        self.bump_source(sources[0]['source_id'], '\nmore。')
        out = self.ev._replay_feedback(base, {'id': 'w1', 'project': 'project'})
        self.assertEqual(out, [{'finding': STALE_FEEDBACK_NOTICE}])

    def test_unknown_alias_is_never_guessed_on_replay(self):
        self.ev._last_reference_map = {'card001': 'a' * 32}
        entries = self.ev._attach_historical_aliases(
            [{'finding': 'the model said card777 but never mentioned another'}],
            self.ev._last_reference_map)
        self.assertNotIn('historical_aliases', entries[0])
        self.assertEqual(entries[0]['unresolved_aliases'], ['card777'])
        # An untyped (multi-role) ``refNNN`` alias is never guessed either.
        ref = self.ev._attach_historical_aliases(
            [{'finding': 'ref004 overlaps'}], {'ref004': 'b' * 32})
        self.assertNotIn('historical_aliases', ref[0])
        self.assertEqual(ref[0]['unresolved_aliases'], ['ref004'])

    def test_reconciliation_identity_survives_feedback_suppression(self):
        base = self._base()
        base['feedback_envelope']['scope_fingerprint'] = 'drifted'
        out = self.ev._replay_feedback(base, {'id': 'w1', 'project': 'project'})
        self.assertEqual(out, [{'finding': STALE_FEEDBACK_NOTICE}])
        # The independent reconciliation responsibility is derived from the
        # persisted accepted-origin store, never from the withheld prose.
        store = {'format': RECONCILIATION_STORE_FORMAT,
                 'targets': [{'card_id': 'd' * 32,
                              'verdict': {'relation': 'duplicate'}}]}
        recon = self.ev._safe_reconciliation_feedback(store)
        self.assertTrue(recon)
        self.assertIn('d' * 12, recon[0]['finding'])


if __name__ == '__main__':
    unittest.main()
