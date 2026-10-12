"""Workspace regression tests; standard library only, no AI/network execution."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from itmanager.store import Store
from itmanager.workspaces import WorkspaceService, builtin_templates, validate_template


class TemplateTests(unittest.TestCase):
    def test_builtins_are_fresh_and_explicitly_non_compliance(self):
        values = builtin_templates()
        self.assertEqual(len(values), 3)
        self.assertIn('非合规保证', values[0]['description'])
        self.assertIn('专业机构确认', values[0]['description'])
        values[0]['phases'][0]['name'] = 'changed'
        self.assertNotEqual(builtin_templates()[0]['phases'][0]['name'], 'changed')

    def test_strict_configuration(self):
        mutations = [
            lambda d: d.update(script='print(1)'),
            lambda d: d.update(url='https://example.com/plugin.py'),
            lambda d: d.update(schema_version=True),
            lambda d: d.update(name='x' * 201),
            lambda d: d['phases'].append(deepcopy(d['phases'][0])),
            lambda d: d['work_packages'][0].update(phase='missing'),
            lambda d: d['forms'][0]['fields'][0].update(type='python'),
            lambda d: d['forms'][0]['fields'][0].update(required='true'),
            lambda d: d['forms'][0]['fields'].append(deepcopy(d['forms'][0]['fields'][0])),
            lambda d: d['forms'][0]['fields'][-1].update(options=['same', 'same']),
            lambda d: d['prompts'][0].update(template='{{content.__class__}}'),
            lambda d: d['prompts'][0].update(template='{{content}} {{unknown}}'),
            lambda d: d['prompts'][0].update(template='{% include "remote" %} {{content}}'),
            lambda d: d['prompts'][0].update(template='no context placeholder'),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                data = builtin_templates()[0]
                mutate(data)
                with self.assertRaises(ValueError):
                    validate_template(data)

    def test_total_size_limit(self):
        data = builtin_templates()[0]
        data['phases'][0]['deliverables'] = ['文' * 2000] * 50
        with self.assertRaises(ValueError):
            validate_template(data)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'test.sqlite3')
        self.service = WorkspaceService(self.store)
        self.templates = self.service.seed_builtin()

    def create(self, suffix='a'):
        return self.service.create_workspace(self.templates[0]['id'], '专项' + suffix,
            '负责人', '2026-01-01', '2026-12-31', 'workspace-test-' + suffix * 16)

    def entry_data(self):
        return {'subject': '资料确认', 'evidence': '证据编号ABC',
                'date': '2026-02-01', 'state': '待确认'}

    def test_seed_history_export_and_snapshot(self):
        self.assertEqual(len(self.service.seed_builtin()), 3)
        workspace = self.create()
        old = self.service.get_template(self.templates[0]['id'])
        config = deepcopy(old['config'])
        config['name'] = '改版'
        edited = self.service.save_template(config, old['id'])
        self.assertEqual(edited['version'], 2)
        self.assertEqual(edited['oldhistory'][0]['config'], old['config'])
        self.assertEqual(self.service.get_workspace(workspace['id'])['template_snapshot'], old['config'])
        self.assertEqual(self.service.export_template(old['id']), config)
        self.assertNotIn('oldhistory', self.service.export_template(old['id']))
        # Same request replays even after template revision.
        self.assertEqual(self.create()['id'], workspace['id'])

    def test_create_is_scoped_and_idempotent(self):
        workspace = self.create()
        self.assertEqual(workspace, self.create())
        records = self.service.related_records(workspace['id'])
        self.assertEqual(len(records), 1 + len(workspace['task_ids']))
        self.assertTrue(all(r['data']['项目ID'] == workspace['project_id']
                            for r in records if r['module'] == 'tasks'))
        with self.assertRaises(ValueError):
            self.service.create_workspace(self.templates[0]['id'], '另一个名称',
                '负责人', '2026-01-01', '2026-12-31', workspace['request_id'])
        self.assertEqual(len(self.store.list_records()), len(records))

    def test_atomic_bundle_failure_does_not_leave_workspace(self):
        workspace = {'id': 'WS-invalid', 'name': '失败', 'project_id': 'P-invalid',
                     'task_ids': ['T-invalid'], 'request_args': {'name': '失败'}}
        records = [{'id': 'P-invalid', 'module': 'projects', 'data': {'名称': '项目'}},
                   {'id': 'T-invalid', 'module': 'tasks',
                    'data': {'名称': '任务', '项目ID': 'missing'}}]
        with self.assertRaises(ValueError):
            self.store.apply_workspace_bundle(workspace, records, 'invalid-workspace-request-0001')
        self.assertFalse(self.store.list_records())
        self.assertIsNone(self.store.get('workspaces', 'WS-invalid'))

    def test_entries_validation_history_and_isolation(self):
        a, b = self.create('a'), self.create('b')
        entry = self.service.save_entry(a['id'], 'evidence', self.entry_data())
        self.assertEqual(len(self.service.list_entries(a['id'])), 1)
        self.assertEqual(self.service.list_entries(b['id']), [])
        with self.assertRaises(ValueError):
            self.service.get_entry(b['id'], entry['id'])
        with self.assertRaises(ValueError):
            self.service.save_entry(b['id'], 'evidence', self.entry_data(), entry['id'], 1)
        for updates in ({'date': '2026-02-30'}, {'state': 'unknown'}, {'subject': ''}, {'subject': 7}, {'extra': 'bad'}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.service.save_entry(a['id'], 'evidence', dict(self.entry_data(), **updates))
        updated = self.service.save_entry(a['id'], 'evidence',
            dict(self.entry_data(), evidence='新证据'), entry['id'], 1, status='submitted')
        self.assertEqual(updated['version'], 2)
        self.assertEqual(updated['oldhistory'][0]['data'], entry['data'])
        with self.assertRaises(ValueError):
            self.service.save_entry(a['id'], 'evidence', self.entry_data(), entry['id'], 1)

    def test_scope_associations_explicit_links_and_report(self):
        a, b = self.create('a'), self.create('b')
        log = self.store.save_record('logs', {'名称': 'A日志', '关联ID': a['task_ids'][0], '工作内容': '本专项日志内容'})
        risk = self.store.save_record('risks', {'名称': 'A风险', '关联ID': a['project_id']})
        unrelated = self.store.save_record('logs', {'名称': '绝不能泄露的B日志', '关联ID': b['project_id']})
        extra = self.store.save_record('collection', {'名称': '显式资料'})
        self.service.link_record(a['id'], extra['id'])
        self.service.link_record(a['id'], extra['id'])
        self.assertEqual(len(self.store.objects('workspace_links')), 1)
        with self.assertRaises(ValueError):
            self.service.link_record(a['id'], 'not-found')
        ids = {r['id'] for r in self.service.related_records(a['id'])}
        self.assertTrue({log['id'], risk['id'], extra['id']} <= ids)
        self.assertNotIn(unrelated['id'], ids)
        self.service.save_entry(a['id'], 'evidence', dict(self.entry_data(), evidence='<script>alert(1)</script>'))
        self.service.save_entry(b['id'], 'evidence', dict(self.entry_data(), evidence='绝不能泄露的B表单'))
        report = self.service.report(a['id'])
        self.assertIn('本专项日志内容', report)
        self.assertNotIn('绝不能泄露', report)
        self.assertNotIn('<script>', report)
        self.assertIn('0/1', report)
        context = self.service.render_context(a['id'])
        self.assertEqual(context['context'], report)
        self.assertEqual(context['title'], a['name'])
        self.assertIn('{{content}}', context['template'])

    def test_completed_phase_and_action_task_membership(self):
        workspace = self.create()
        tid = workspace['task_ids'][0]
        task = next(r for r in self.store.list_records('tasks') if r['id'] == tid)
        self.store.save_record('tasks', dict(task['data'], 状态='已完成', 完成证据='凭据', 验收人='业务负责人'), tid, task['version'])
        progress = self.service.phase_progress(workspace['id'])
        self.assertEqual(progress[0]['percent'], 100)
        actions = self.store.create_actions(workspace['project_id'], [{'name': '追加行动'}], 'workspace-action-request-0001')
        ids = {r['id'] for r in self.service.related_records(workspace['id'])}
        self.assertIn(actions['tasks'][0]['id'], ids)
        self.assertIn(actions['logs'][0]['id'], ids)


if __name__ == '__main__':
    unittest.main()
