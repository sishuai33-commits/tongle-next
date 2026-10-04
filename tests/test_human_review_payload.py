import json
import unittest
from unittest.mock import patch
import test_evolution_consumer as base

class HumanReviewPayloadTests(unittest.TestCase):
    setUp=base.EvolutionConsumerTests.setUp
    write_cfg=base.EvolutionConsumerTests.write_cfg
    def test_real_request_keeps_full_bodies_and_names_only_manual_targets(self):
        cards=[dict(id='a',version=2,body_sha256='hash-a',body='人工边界：不得动正式环境。',manually_edited=True),
               dict(id='b',version=3,body_sha256='hash-b',body='未修改的人工卡',manually_edited=True),
               dict(id='c',version=1,body_sha256='hash-c',body='普通卡',manually_edited=False)]
        payload={'task':'verify','existing':{'cards':cards},'proposal':{'operations':[{'op':'update','id':'a','body':'新候选'}, {'op':'retire','id':'c'}]}}
        original=json.loads(json.dumps(payload));sent=[]
        response={'approved':True,'findings':[]}
        def invoke(command,prompt,timeout,env=None):sent.append(json.loads(prompt));return response,{'input':1,'output':1}
        with patch('tongle.worker.invoke_json',side_effect=invoke):
            result=self.ev._call({'id':'probe'},'verify',payload,{'model_command':['fake']})
        self.assertEqual(sent[0]['existing']['cards'],cards)
        self.assertEqual(sent[0]['required_human_checks'],[{'id':'a','version':2,'body_sha256':'hash-a'}])
        self.assertEqual(payload,original)
        self.assertEqual(result,response)
        self.assertNotIn('human_checks',result) # Missing model judgment is never manufactured.
    def test_compile_does_not_gain_preservation_claims(self):
        seen=[]
        def invoke(command,prompt,timeout,env=None):seen.append(json.loads(prompt));return {'operations':[]},{'input':1,'output':1}
        payload={'task':'compile','existing':{'cards':[]}}
        with patch('tongle.worker.invoke_json',side_effect=invoke):self.ev._call({'id':'p'},'compile',payload,{'model_command':['fake']})
        self.assertEqual(seen[0],payload)

    def test_hex_identity_roundtrip_compile_then_verify(self):
        cid='a'*32
        card={'id':cid,'version':2,'body_sha256':'b'*64,'body':'人工条件完整保留。','manually_edited':True}
        base={'task':'compile','existing':{'cards':[card]}}
        seen=[]
        def invoke(command,prompt,timeout,env=None):
            data=json.loads(prompt);seen.append(data)
            alias=data['existing']['cards'][0]['id']
            self.assertNotEqual(alias,cid)
            if 'proposal' not in data:
                return {'operations':[{'op':'update','id':alias,'expected_version':2,'body':'人工条件完整保留。新增。'}]},{'input':1,'output':1}
            self.assertEqual(data['proposal']['operations'][0]['id'],alias)
            self.assertEqual(data['required_human_checks'][0]['id'],alias)
            return {'approved':True,'findings':[],'human_checks':[{'id':alias,'version':2,'body_sha256':'b'*64,'preserved':True,'reason':'全文已核对'}]},{'input':1,'output':1}
        with patch('tongle.worker.invoke_json',side_effect=invoke):
            proposal=self.ev._call({'id':'p'},'compile',base,{'model_command':['fake']})
            self.assertEqual(proposal['operations'][0]['id'],cid)
            review=self.ev._call({'id':'p'},'verify',dict(base,proposal=proposal),{'model_command':['fake']})
        self.assertEqual(review['human_checks'][0]['id'],cid)
        self.assertEqual(seen[1]['existing']['cards'][0]['body'],card['body'])
        self.assertEqual(seen[1]['existing']['cards'][0]['body_sha256'],card['body_sha256'])
