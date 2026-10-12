"""Explicit audit repro: run python tests/audit_backend_repro.py; not pytest-collected.
Only temporary SQLite files; model calls mocked; no production data or source changes.
Assertions demonstrate current deficiencies, not desired fixed behavior.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from itmanager.server import create_app
from itmanager.store import Store
from itmanager.workspaces import WorkspaceService


class BackendAuditRepro(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'ITMANAGER_DB_PATH': str(Path(self.temp.name) / 'audit.sqlite3'), 'ITM_DB_BACKEND': 'sqlite'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = create_app(self.temp.name, 'audit-local-token-' + 'x' * 32)
        self.client = TestClient(self.app, headers={'Authorization': 'Bearer audit-local-token-' + 'x' * 32}, raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.store = self.app.state.store

    def workspace(self):
        svc = self.app.state.workspaces
        template = svc.list_templates()[0]
        return svc.create_workspace(template['id'], 'Audit', 'Owner', '2030-01-01', '2030-12-31', 'audit-workspace-request-0001')

    def prompt(self):
        return next(x for x in self.store.objects('prompts') if x.get('module') == 'tasks' and x.get('purpose') == 'form_fill')

    def test_entry_cross_store_loses_one_accepted_revision(self):
        ws = self.workspace()
        data = {'subject': 'initial', 'evidence': 'evidence', 'date': '2030-01-01', 'state': '待确认'}
        first = self.app.state.workspaces.save_entry(ws['id'], 'evidence', data)
        services = [WorkspaceService(Store(self.store.path)), WorkspaceService(Store(self.store.path))]
        barrier = Barrier(2)
        # Force both independent stores to finish reading v1 before either writes.
        # No production method semantics changed; this only controls interleaving.
        for service in services:
            original = service.get_entry
            def synchronized(*args, original=original):
                value = original(*args)
                barrier.wait(timeout=10)
                return value
            service.get_entry = synchronized
        def save(index):
            return services[index].save_entry(ws['id'], 'evidence', dict(data, subject=f'writer-{index}'), first['id'], 1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, [0, 1]))
        final = self.store.get('workspace_entries', first['id'])
        self.assertEqual([x['version'] for x in results], [2, 2])
        self.assertEqual(final['version'], 2)
        self.assertEqual(len(final['oldhistory']), 1)
        self.assertEqual(final['oldhistory'][0]['data']['subject'], 'initial')
        print('ENTRY_RACE: both accepted version=2; one accepted value absent from current/history')

    def test_prompt_default_has_no_version_or_history(self):
        old = self.prompt()
        data = {k: v for k, v in old.items() if k != 'id'}
        data['name'] = 'New default'
        result = self.client.post('/objects/prompts', json={'data': data})
        self.assertEqual(result.status_code, 200)
        current = self.store.get('prompts', old['id'])
        self.assertFalse(current['is_default'])
        self.assertEqual(current['version'], old['version'])
        self.assertFalse([x for x in self.store.objects('prompt_history') if x.get('prompt_id') == old['id']])
        print('DEFAULT_HISTORY: old default mutated without version/history')

    def test_failed_prompt_write_still_mutates_defaults_and_history(self):
        old = self.prompt()
        data = dict(old, id='wrong-payload-id', name='Rejected new default')
        before = len(self.store.objects('prompts'))
        # Missing envelope id means create; a payload id must fail final Store.put.
        response = self.client.post('/objects/prompts', json={'data': data})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.store.objects('prompts')), before)
        self.assertFalse(self.store.get('prompts', old['id'])['is_default'])
        # The same mismatch on edit appends a phantom history row before rejection.
        response = self.client.post('/objects/prompts', json={'id': old['id'], 'data': data})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.get('prompts', old['id'])['version'], 1)
        self.assertEqual(len(self.store.objects('prompt_history')), 1)
        print('FAILED_PROMPT: HTTP400 persisted default demotion; rejected edit added history')

    def test_form_ai_sends_changed_context_without_preview_binding(self):
        old = self.store.save_record('tasks', {'名称': 'Preview public', '管理建议': 'Public text'})
        provider = self.store.put('providers', {'provider': 'deepseek', 'model': 'mock', 'enabled': True})
        # GUI preview uses old local records. A second client updates before POST.
        self.store.save_record('tasks', {'名称': 'Preview public', '管理建议': 'PRIVATE_AFTER_PREVIEW'}, old['id'], 1)
        body = {'provider_id': provider['id'], 'prompt_id': self.prompt()['id'], 'module': 'tasks', 'basis': '', 'current': {}, 'context_ids': [old['id']], 'confirmed': True, 'sensitive_confirmed': False}
        reply = {'text': json.dumps({'fields': {}, 'suggestions': [], 'warnings': []}), 'model': 'mock'}
        with patch('itmanager.ai.generate', return_value=reply) as generate:
            response = self.client.post('/ai/form-fill', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('PRIVATE_AFTER_PREVIEW', generate.call_args.args[2])
        print('AI_PREVIEW: changed text sent with old ID-only consent, no sensitive confirmation')

    def test_template_stale_editor_silently_overwrites(self):
        template = self.app.state.workspaces.list_templates()[0]
        a, b = deepcopy(template['config']), deepcopy(template['config'])
        a['description'] = 'A independently changed description'
        b['name'] = 'B stale editor name'
        first = self.client.post('/workspace-templates', json={'id': template['id'], 'data': a})
        second = self.client.post('/workspace-templates', json={'id': template['id'], 'data': b})
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(second.json()['version'], 3)
        self.assertEqual(second.json()['config']['description'], template['config']['description'])
        print('TEMPLATE_STALE: sequential stale editor overwrote description, both accepted')

    def test_malformed_inputs_return_500(self):
        provider = self.store.put('providers', {'provider': 'deepseek', 'model': 'mock'})
        prompt = next(x for x in self.store.objects('prompts') if not x.get('purpose'))
        payloads = [('/ai/generate', {'provider_id': provider['id'], 'prompt_id': prompt['id'], 'record_ids': [{}], 'confirmed': True}),
                    ('/objects/schedules', {'data': {'kind': 'report', 'mode': 'draft', 'schedule': {}, 'period_days': None}})]
        for path, body in payloads:
            response = self.client.post(path, json=body)
            self.assertEqual(response.status_code, 500, (path, response.text))
        print('INPUT_TYPES: record_ids=[{}] and period_days=null -> HTTP500')


if __name__ == '__main__':
    unittest.main(verbosity=2)
