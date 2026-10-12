"""Regression tests for atomic object history, defaults and workspace CAS."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from itmanager.server import create_app
from itmanager.store import Store
from itmanager.workspaces import WorkspaceService


def test_cross_store_entry_compare_and_swap(tmp_path):
    store = Store(tmp_path / 'db.sqlite3')
    svc = WorkspaceService(store)
    template = svc.seed_builtin()[0]
    ws = svc.create_workspace(template['id'], 'WS', 'Owner', '2030-01-01', '2030-12-31', 'audit-fixed-entry-0001')
    data = {'subject': 'initial', 'evidence': 'evidence', 'date': '2030-01-01', 'state': '待确认'}
    entry = svc.save_entry(ws['id'], 'evidence', data)
    services = [WorkspaceService(Store(store.path)), WorkspaceService(Store(store.path))]
    barrier = Barrier(2)
    for service in services:
        original = service.get_entry
        def synchronized(*args, original=original):
            result = original(*args)
            barrier.wait(timeout=10)
            return result
        service.get_entry = synchronized
    def save(index):
        try:
            return services[index].save_entry(ws['id'], 'evidence', dict(data, subject=str(index)), entry['id'], 1)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, [0, 1]))
    assert sum(x is not None for x in results) == 1
    current = store.get('workspace_entries', entry['id'])
    assert current['version'] == 2 and len(current['oldhistory']) == 1
    assert current['oldhistory'][0]['data'] == data


def test_template_expected_version_and_legacy_compatibility(tmp_path):
    svc = WorkspaceService(Store(tmp_path / 'db.sqlite3'))
    old = svc.seed_builtin()[0]
    a, b = deepcopy(old['config']), deepcopy(old['config'])
    a['description'] = 'A edit'
    b['name'] = 'B stale'
    svc.save_template(a, old['id'], 1)
    with pytest.raises(ValueError, match='更新'):
        svc.save_template(b, old['id'], 1)
    assert svc.get_template(old['id'])['config'] == a
    legacy = svc.save_template(b, old['id'])
    assert legacy['version'] == 3 and len(legacy['oldhistory']) == 2


def test_prompt_default_atomicity_and_history(tmp_path):
    app = create_app(tmp_path, 'x' * 32)
    store = app.state.store
    with TestClient(app, headers={'Authorization': 'Bearer ' + 'x' * 32}) as client:
        old = next(x for x in store.objects('prompts') if x.get('module') == 'tasks')
        malformed = dict(old, id='wrong', name='reject')
        before = store.objects('prompts'), store.objects('prompt_history'), store.audit_rows()
        assert client.post('/objects/prompts', json={'data': malformed}).status_code == 400
        assert client.post('/objects/prompts', json={'id': old['id'], 'data': malformed}).status_code == 400
        assert before == (store.objects('prompts'), store.objects('prompt_history'), store.audit_rows())
        new = {k: v for k, v in old.items() if k != 'id'}
        new['name'] = 'New default'
        response = client.post('/objects/prompts', json={'data': new})
        assert response.status_code == 200
        changed = store.get('prompts', old['id'])
        assert changed['version'] == 2 and changed['is_default'] is False
        histories = [x for x in store.objects('prompt_history') if x['prompt_id'] == old['id']]
        assert len(histories) == 1 and histories[0]['is_default'] is True
        assert client.post('/objects/prompts', json={'id': old['id'], 'data': old}).status_code == 400
        # Fault after a default demotion must roll back every object/history/audit write.
        before = store.objects('prompts'), store.objects('prompt_history'), store.audit_rows()
        original = store.audit
        def fail(db, action, oid, detail=''):
            original(db, action, oid, detail)
            raise RuntimeError('injected transaction failure')
        with patch.object(store, 'audit', side_effect=fail), pytest.raises(RuntimeError):
            store.save_versioned_object('prompts', new, prompt_defaults=True)
        assert before == (store.objects('prompts'), store.objects('prompt_history'), store.audit_rows())


def test_concurrent_default_switch_is_unique(tmp_path):
    path = tmp_path / 'db.sqlite3'
    stores = [Store(path), Store(path)]
    barrier = Barrier(2)
    def save(index):
        barrier.wait(timeout=10)
        return stores[index].save_versioned_object('prompts', {'name': str(index), 'template': '{{basis}}', 'purpose': 'form_fill', 'module': 'tasks', 'is_default': True}, prompt_defaults=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(save, [0, 1]))
    rows = stores[0].objects('prompts')
    assert sum(x['is_default'] for x in rows) == 1
    assert sorted(x['version'] for x in rows) == [1, 2]
    assert len(stores[0].objects('prompt_history')) == 1


@pytest.mark.parametrize('days', [None, True, '7', 1.5, 0, 367])
def test_schedule_period_is_strict_integer(tmp_path, days):
    app = create_app(tmp_path, 'x' * 32)
    with TestClient(app, headers={'Authorization': 'Bearer ' + 'x' * 32}) as client:
        response = client.post('/objects/schedules', json={'data': {'kind': 'report', 'mode': 'draft', 'schedule': {}, 'period_days': days}})
        assert response.status_code == 400
        assert not app.state.store.objects('schedules')
