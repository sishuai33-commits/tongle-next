"""Source dependency regressions. Controlled record fixtures, not model quality tests."""
import json
import unittest
import test_acceptance as base

class SynthesisProvenance(unittest.TestCase):
    setUp=base.Acceptance.setUp
    tearDown=base.Acceptance.tearDown
    call=base.Acceptance.call
    query=base.Acceptance.query
    def prepare(self):
        self.a=self.base/'a.md';self.b=self.base/'b.md'
        self.a.write_text('实时告警不能使用缓存。');self.b.write_text('缓存只能作为历史参考。')
        self.call('ingest','--project','study','--path',str(self.a),'--source-id','a')
        self.call('ingest','--project','study','--path',str(self.b),'--source-id','b')
        self.payload={'text':'实时告警不能使用缓存；缓存只能作为历史参考。','quote':self.a.read_text(),'kind':'synthesis',
                      'evidence':[{'source_id':'a','version':1,'quote':self.a.read_text()},{'source_id':'b','version':1,'quote':self.b.read_text()}]}
        return self.call('record','--project','study','--key','synthesis','--status','candidate','--source-id','a','--source-version','1',text=json.dumps(self.payload))
    def test_secondary_revision_invalidates_synthesis(self):
        self.prepare();self.assertTrue(self.query('study','历史参考'))
        self.b.write_text('缓存必须标明采样时间。')
        self.call('ingest','--project','study','--path',str(self.b),'--source-id','b')
        self.assertFalse(self.query('study','历史参考'))
    def test_secondary_missing_and_revoke_filter_synthesis(self):
        self.prepare()
        self.call('source-state','--project','study','--id','b','--state','missing')
        self.assertFalse(self.query('study','缓存'))
        self.call('source-state','--project','study','--id','b','--state','available')
        self.assertTrue(self.query('study','缓存'))
        self.call('source-state','--project','study','--id','b','--state','revoked')
        self.assertFalse(self.query('study','缓存'))
    def test_invalid_secondary_quote_rejected_before_record_creation(self):
        self.prepare();self.payload['evidence'][1]['quote']='不在资料中的结论'
        self.call('record','--project','study','--key','bad-synthesis','--status','candidate','--source-id','a','--source-version','1',text=json.dumps(self.payload),ok=False)
