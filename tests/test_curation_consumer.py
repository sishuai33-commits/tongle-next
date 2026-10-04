"""Independent consumer tests for migration planning, using synthetic local assets only."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tongle.curation import inventory, validate_plan


class CurationConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.vault = self.base / 'vault'
        self.vault.mkdir()

    def write(self, path, body):
        p = self.vault / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)

    def plan(self, m):
        return {'schema_version': 1, 'manifest_digest': m['manifest_digest'],
                'decisions': [{'path': p, 'expected_sha256': i['sha256'], 'action': 'keep',
                               'target': p, 'reason': 'retain original meaning',
                               'review_status': 'content_reviewed'} for p, i in m['items'].items()]}

    def test_unchanged_nested_note_is_structurally_valid(self):
        self.write('知识/投资/框架.md', '# 框架\nA condition and its limits.\n')
        m = inventory(self.vault)
        self.assertTrue(validate_plan(self.vault, m, self.plan(m))['readiness']['structurally_valid'])

    def test_decision_wrong_expected_hash_is_blocked(self):
        self.write('框架.md', '# 框架\n')
        m = inventory(self.vault); p = self.plan(m)
        p['decisions'][0]['expected_sha256'] = '0' * 64
        self.assertFalse(validate_plan(self.vault, m, p)['readiness']['ready_for_application'])

    def test_explicit_wrong_directory_not_healed_by_basename(self):
        self.write('知识/正确/框架.md', '# 框架\n')
        self.write('索引.md', '[[知识/错误/框架]]\n')
        m = inventory(self.vault)
        self.assertEqual(m['items']['索引.md']['links'][0]['status'], 'broken')

    def test_percent_encoded_chinese_target_is_utf8(self):
        self.write('知识/框架.md', '# 框架\n')
        self.write('索引.md', '[框架](%E7%9F%A5%E8%AF%86/%E6%A1%86%E6%9E%B6.md)\n')
        m = inventory(self.vault)
        self.assertEqual(m['items']['索引.md']['links'][0]['resolved_path'], '知识/框架.md')

    def test_inventory_rejects_symlink_ancestor(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.vault, target_is_directory=True)
        (self.vault / '子库').mkdir()
        with self.assertRaises((ValueError, OSError)):
            inventory(alias / '子库')

    def test_hidden_managed_metadata_requires_db_sync(self):
        self.write('记忆.md', '%%\ntongle-meta: ' + json.dumps({
            'id': 'a' * 32, 'project': 'demo', 'source_id': 'b' * 32,
            'source_version': 1}) + '\n%%\n\n# 工作范围\nScope statement.\n')
        m = inventory(self.vault)
        self.assertFalse(validate_plan(self.vault, m, self.plan(m))['readiness']['ready_for_application'])

    def test_archive_of_managed_note_cannot_be_ready(self):
        self.write('记忆.md', '---\ntongle_id: ' + 'a' * 32 + '\n---\n# 工作范围\n')
        m = inventory(self.vault); p = self.plan(m)
        p['decisions'][0].update(action='archive', target='历史.md')
        self.assertFalse(validate_plan(self.vault, m, p)['readiness']['ready_for_application'])

    def test_archiving_target_requires_incoming_link_update(self):
        self.write('知识.md', '# 历史判断\n')
        self.write('首页.md', '[[知识]]\n')
        m = inventory(self.vault); p = self.plan(m)
        next(d for d in p['decisions'] if d['path'] == '知识.md').update(action='archive', target='旧知识.md')
        self.assertGreater(validate_plan(self.vault, m, p)['summary']['unaddressed_links'], 0)

    def test_malformed_manifest_fails_closed_without_exception(self):
        self.write('框架.md', '# 框架\n')
        m = inventory(self.vault); p = self.plan(m)
        del m['items']['框架.md']['sha256']
        result = validate_plan(self.vault, m, p)
        self.assertFalse(result['readiness']['ready_for_application'])

    def test_dotdot_target_is_not_normalized_away(self):
        self.write('框架.md', '# 框架\n')
        m = inventory(self.vault); p = self.plan(m)
        p['decisions'][0].update(action='move', target='知识/../新框架.md')
        self.assertFalse(validate_plan(self.vault, m, p)['readiness']['structurally_valid'])

    def test_two_cards_cannot_target_file_and_child(self):
        self.write('甲.md', '# 甲\n'); self.write('乙.md', '# 乙\n')
        m = inventory(self.vault); p = self.plan(m)
        p['decisions'][0].update(action='move', target='知识')
        p['decisions'][1].update(action='move', target='知识/乙.md')
        self.assertFalse(validate_plan(self.vault, m, p)['readiness']['structurally_valid'])

    def test_claimed_rewrite_to_removed_path_is_not_accepted(self):
        self.write('旧目录/要点.md', '# 要点\n')
        self.write('首页.md', '[[旧目录/要点]]\n')
        m = inventory(self.vault); p = self.plan(m)
        next(d for d in p['decisions'] if d['path'] == '旧目录/要点.md').update(
            action='move', target='新目录/要点.md')
        p['link_updates'] = [{'from_path': '首页.md', 'old_target': '旧目录/要点',
                              'new_target': '旧目录/要点'}]
        self.assertFalse(validate_plan(self.vault, m, p)['readiness']['structurally_valid'])

    def test_moving_linking_note_does_not_preserve_relative_markdown_link(self):
        self.write('旧目录/来源.md', '# 依据\n')
        self.write('旧目录/判断.md', '[引用](来源.md)\n')
        m = inventory(self.vault); p = self.plan(m)
        next(d for d in p['decisions'] if d['path'] == '旧目录/判断.md').update(
            action='move', target='新目录/判断.md')
        self.assertFalse(validate_plan(self.vault, m, p)['readiness']['structurally_valid'])

    def test_relative_rewrite_from_new_source_location_is_accepted(self):
        self.write('旧目录/来源.md', '# 依据\n')
        self.write('旧目录/判断.md', '[引用](来源.md#依据)\n')
        m = inventory(self.vault); p = self.plan(m)
        next(d for d in p['decisions'] if d['path'] == '旧目录/判断.md').update(
            action='move', target='新目录/判断.md')
        p['link_updates'] = [{'from_path': '旧目录/判断.md', 'old_target': '来源.md#依据',
                              'new_target': '../旧目录/来源.md#依据'}]
        self.assertTrue(validate_plan(self.vault, m, p)['readiness']['structurally_valid'])

    def test_current_note_cannot_claim_historical_exception(self):
        self.write('知识.md', '# 知识\n'); self.write('首页.md', '[[知识]]\n')
        m = inventory(self.vault); p = self.plan(m)
        next(d for d in p['decisions'] if d['path'] == '知识.md').update(action='archive', target='旧知识.md')
        p['preserve_historical'] = [{'from_path': '首页.md', 'old_target': '知识',
                                     'preserve_historical': True, 'reason': 'history'}]
        self.assertFalse(validate_plan(self.vault, m, p)['readiness']['structurally_valid'])

    def test_archived_snapshot_may_preserve_historical_link_explicitly(self):
        self.write('知识.md', '# 知识\n'); self.write('归档/旧首页.md', '[[知识]]\n')
        m = inventory(self.vault); p = self.plan(m)
        next(d for d in p['decisions'] if d['path'] == '知识.md').update(action='archive', target='旧知识.md')
        p['preserve_historical'] = [{'from_path': '归档/旧首页.md', 'old_target': '知识',
                                     'preserve_historical': True, 'reason': 'frozen historical snapshot'}]
        result = validate_plan(self.vault, m, p)
        self.assertTrue(result['readiness']['structurally_valid'])
        self.assertEqual(result['link_report'][0]['status'], 'preserved_historical')

    def test_nonstring_action_and_review_fail_closed(self):
        self.write('知识.md', '# 知识\n'); m = inventory(self.vault)
        for field in ('action', 'review_status'):
            with self.subTest(field=field):
                p = self.plan(m); p['decisions'][0][field] = []
                self.assertFalse(validate_plan(self.vault, m, p)['readiness']['ready_for_application'])

    def test_partial_inventory_cannot_be_ready(self):
        self.write('知识.md', '# 知识\n'); m = inventory(self.vault); p = self.plan(m)
        partial = dict(m, issues=[{'code': 'walk_error'}])
        with patch('tongle.curation.inventory', return_value=partial):
            self.assertFalse(validate_plan(self.vault, m, p)['readiness']['ready_for_application'])

    def test_target_symlink_or_hidden_path_cannot_be_published(self):
        self.write('知识.md', '# 知识\n'); m = inventory(self.vault)
        (self.vault / '链接.md').symlink_to(self.vault / '知识.md')
        for target in ('链接.md', '.私有/知识.md', '资料/./知识.md'):
            with self.subTest(target=target):
                p = self.plan(m); p['decisions'][0].update(action='move', target=target)
                self.assertFalse(validate_plan(self.vault, m, p)['readiness']['ready_for_application'])


if __name__ == '__main__':
    unittest.main()
