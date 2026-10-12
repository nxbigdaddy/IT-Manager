"""Atomic management operations, immutable receipts and exception remediation."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from itmanager.repository import Repository
from itmanager.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'management.sqlite3')


def key(n=1):
    return 'management-request-' + str(n)


def task(store, **fields):
    return store.save_record('tasks', {'名称': '交付任务', '状态': '未开始', **fields})


def counts(store):
    with store.connect() as db:
        return tuple(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                     for table in ('records', 'record_history', 'audit', 'objects'))


def test_task_cas_replay_and_log(store):
    old = task(store)
    payload = {'next_step': '部署预发布', 'status': '进行中', 'evidence': '测试报告'}
    result = store.task_action(old['id'], 1, 'progress', payload, key())
    assert isinstance(store, Repository)
    assert result['task']['version'] == 2
    assert result['task']['data']['状态'] == '进行中'
    log = result['log']['data']
    assert log['关联ID'] == old['id']
    assert all(log[k] for k in ('名称', '日期', '工作内容', '结果', '下一步', '证据'))
    snapshot = counts(store)
    assert store.task_action(old['id'], 1, 'progress', payload, key()) == result
    assert counts(store) == snapshot
    with pytest.raises(ValueError, match='刷新'):
        store.task_action(old['id'], 1, 'progress', payload, key(2))
    with pytest.raises(ValueError, match='不同操作'):
        store.task_action(old['id'], 2, 'progress', payload, key())
    assert counts(store) == snapshot
    assert len(store.objects('operation_receipts')) == 1


def test_actions_lifecycle_and_postpone_history(store):
    old = task(store, 截止日期='2026-06-01')
    blocked = store.task_action(old['id'], 1, 'block', {'reason': '等待审批', 'next_step': '请求审批'}, key())['task']
    assert blocked['data']['状态'] == '阻塞'
    result = store.task_action(old['id'], 2, 'postpone', {'due_date': '2026-06-30', 'reason': '批准延期', 'approver': '主管'}, key(2))
    detail = json.loads(result['log']['data']['工作内容'])
    assert detail['original_due_date'] == '2026-06-01'
    assert detail['new_due_date'] == '2026-06-30'
    assert detail['approver'] == '主管'
    assert store.history(old['id'])[-2]['data']['截止日期'] == '2026-06-01'
    submitted = store.task_action(old['id'], 3, 'submit', {'evidence': '验收报告', 'acceptor': '甲'}, key(3))
    assert submitted['task']['data']['状态'] == '待验收'
    done = store.task_action(old['id'], 4, 'complete', {}, key(4))
    assert done['task']['data']['状态'] == '已完成'
    assert done['log']['data']['证据'] == '验收报告'
    with pytest.raises(ValueError, match='已完成'):
        store.task_action(old['id'], 5, 'progress', {'next_step': '继续'}, key(5))
    assert store.task_action(old['id'], 4, 'complete', {}, key(4)) == done


@pytest.mark.parametrize('action,payload', [
    ('progress', {'next_step': 'x', 'status': '已完成'}),
    ('progress', {'next_step': 1}), ('progress', {'next_step': 'x', 'unknown': 'x'}),
    ('block', {'reason': '', 'next_step': 'x'}), ('block', {'reason': 'x'}),
    ('submit', {'evidence': 'x'}), ('complete', {}),
    ('postpone', {'due_date': '2026-02-30', 'reason': 'x', 'approver': '甲'}),
    ('postpone', {'due_date': '2026-02-01', 'reason': 'x'}),
])
def test_task_invalid_has_no_side_effects(store, action, payload):
    old = task(store)
    before = counts(store)
    with pytest.raises(ValueError):
        store.task_action(old['id'], 1, action, payload, key())
    assert counts(store) == before


def test_task_rejects_archived_cancelled_and_non_task(store):
    cancelled = task(store, 状态='已取消')
    archived = task(store)
    store.archive_record(archived['id'], 1)
    project = store.save_record('projects', {'名称': '项目'})
    for row in (cancelled, archived, project):
        with pytest.raises(ValueError):
            store.task_action(row['id'], row['version'], 'progress', {'next_step': 'x'}, key())


def test_create_actions_source_links_logs_and_replay(store):
    project = store.save_record('projects', {'名称': '项目'})
    parent = task(store, 项目ID=project['id'])
    items = [{'name': '行动', 'owner': '甲', 'due_date': '2026-08-01', 'next_step': '验证', 'acceptance': '通过测试'}]
    result = store.create_actions(parent['id'], items, key())
    child = result['tasks'][0]
    assert child['data']['父任务ID'] == parent['id']
    assert child['data']['项目ID'] == project['id']
    assert parent['id'] in child['data']['管理建议']
    assert result['logs'][0]['data']['关联ID'] == child['id']
    before = counts(store)
    assert store.create_actions(parent['id'], items, key()) == result
    assert counts(store) == before
    assert store.create_actions(project['id'], [{'name': '项目行动'}], key(2))['tasks'][0]['data']['项目ID'] == project['id']


def test_ai_source_and_action_batch_rollback(store):
    with pytest.raises(ValueError, match='来源'):
        store.create_actions('ai:missing', [{'name': '行动'}], key())
    call = store.put('ai_calls', {'content': '分析'})
    source_id = 'ai:' + call['id']
    before = counts(store)
    for items in ([], [{'name': 'x'}] * 21, [{'name': '有效'}, {'name': ''}],
                  [{'name': '有效'}, {'name': '日期错', 'due_date': '2026-02-30'}],
                  [{'name': 'x', 'owner': []}], [{'name': 'x', '项目ID': 'injected'}]):
        with pytest.raises(ValueError):
            store.create_actions(source_id, items, key())
        assert counts(store) == before
    result = store.create_actions(source_id, [{'name': '行动'}], key())
    assert source_id in result['tasks'][0]['data']['管理建议']


def checklist():
    return {'name': '月度检查', 'owner': '甲', 'date': '2026-06-01', 'items': [
        {'name': '备份', 'result': '通过'}, {'name': '权限', 'result': '异常', 'note': '过期权限未撤回'},
        {'name': '异地设备', 'result': '不适用'}]}


def test_checklist_exception_closed_loop_and_immutable_replay(store):
    data = checklist()
    result = store.complete_checklist(data, key())
    run, tasks, risks = result['run'], result['tasks'], result['risks']
    assert len(tasks) == len(risks) == 1
    child, risk = tasks[0], risks[0]
    assert run['task_ids'] == [child['id']]
    assert run['risk_ids'] == [risk['id']]
    assert run['id'] in child['data']['管理建议']
    assert risk['id'] in child['data']['管理建议']
    assert risk['data']['关联ID'] == child['id']
    assert child['id'] in risk['data']['长期措施']
    assert store.list_records('logs')[0]['data']['关联ID'] == child['id']
    assert store.get('checklist_runs', run['id']) == run
    before = counts(store)
    assert store.complete_checklist(data, key()) == result
    assert counts(store) == before
    for kind, oid in [('checklist_runs', run['id']), ('operation_receipts', key())]:
        with pytest.raises(ValueError, match='不可修改'):
            store.put(kind, {'name': '篡改'}, oid)
    store.task_action(child['id'], 1, 'complete', {'evidence': '撤权复核', 'acceptor': '乙'}, key(2))
    assert store.get('checklist_runs', run['id']) == run


@pytest.mark.parametrize('bad', [
    {'name': '异常', 'result': '异常'}, {'name': '异常', 'result': '异常', 'note': ' '},
    {'name': '结果', 'result': 'invalid'}, {'name': 'x', 'result': '通过', 'note': 4},
    {'name': 'x', 'result': '通过', 'unknown': ''},
])
def test_checklist_batch_validation_rolls_back(store, bad):
    data = checklist()
    data['items'].append(bad)
    before = counts(store)
    with pytest.raises(ValueError):
        store.complete_checklist(data, key())
    assert counts(store) == before


def test_failure_during_writes_rolls_back_everything(store, monkeypatch):
    data = checklist()
    original = store._snapshot
    def fail(db, oid):
        original(db, oid)
        if oid.startswith('RISKS-'):
            raise RuntimeError('injected failure')
    monkeypatch.setattr(store, '_snapshot', fail)
    before = counts(store)
    with pytest.raises(RuntimeError, match='injected'):
        store.complete_checklist(data, key())
    assert counts(store) == before


def test_task_and_action_log_failure_rollback(store, monkeypatch):
    old = task(store)
    original = store._snapshot
    def fail(db, oid):
        original(db, oid)
        if oid.startswith('LOGS-'):
            raise RuntimeError('log failure')
    monkeypatch.setattr(store, '_snapshot', fail)
    before = counts(store)
    with pytest.raises(RuntimeError, match='log failure'):
        store.task_action(old['id'], 1, 'progress', {'next_step': '实施'}, key())
    assert counts(store) == before
    with pytest.raises(RuntimeError, match='log failure'):
        store.create_actions(old['id'], [{'name': '行动'}], key(2))
    assert counts(store) == before


@pytest.mark.parametrize('request_id', [None, '', 'x' * 15, 'x' * 101, ' ' * 16, '../' * 10])
def test_request_key_validation(store, request_id):
    with pytest.raises(ValueError):
        store.complete_checklist(checklist(), request_id)
    assert counts(store) == (0, 0, 0, 0)


def test_workspace_bundle_forward_refs_replay_and_no_overwrite(store):
    workspace = {'id': 'workspace-1', 'project_id': 'project-1', 'task_ids': ['task-1'], 'config': {'mode': '专项'}}
    rows = [
        {'id': 'task-1', 'module': 'tasks', 'data': {'名称': '任务', '项目ID': 'project-1'}},
        {'id': 'project-1', 'module': 'projects', 'data': {'名称': '专项'}},
    ]
    result = store.apply_workspace_bundle(workspace, rows, key())
    assert result['workspace'] == store.get('workspaces', 'workspace-1')
    assert len(result['records']) == 2
    before = counts(store)
    assert store.apply_workspace_bundle(workspace, rows, key()) == result
    with pytest.raises(ValueError, match='已存在'):
        store.apply_workspace_bundle(workspace, rows, key(2))
    assert counts(store) == before


def test_workspace_retry_uses_user_arguments_not_regenerated_timestamps(store):
    args = {'template_id': 'audit', 'name': '专项', 'owner': '甲', 'start_date': '2026-01-01', 'due_date': '2026-02-01'}
    workspace = {'id': 'stable-workspace', 'request_id': key(), 'request_args': args, 'created': 'original'}
    rows = [{'id': 'stable-project', 'module': 'projects', 'data': {'名称': '专项'}}]
    result = store.apply_workspace_bundle(workspace, rows, key())
    assert store.apply_workspace_bundle(dict(workspace, created='regenerated'), rows, key()) == result
    with pytest.raises(ValueError, match='不同操作'):
        store.apply_workspace_bundle(dict(workspace, request_args=dict(args, owner='乙')), rows, key())


def test_workspace_bundle_validation_and_write_failure_rollback(store, monkeypatch):
    rows = [{'id': 'task-1', 'module': 'tasks', 'data': {'名称': '任务', '项目ID': 'missing'}}]
    with pytest.raises(ValueError, match='校验'):
        store.apply_workspace_bundle({'id': 'workspace-1'}, rows, key())
    assert counts(store) == (0, 0, 0, 0)
    rows[0]['data'].pop('项目ID')
    original = store._operation_receipt
    def fail(*args):
        original(*args)
        raise RuntimeError('receipt failure')
    monkeypatch.setattr(store, '_operation_receipt', fail)
    with pytest.raises(RuntimeError, match='receipt failure'):
        store.apply_workspace_bundle({'id': 'workspace-1'}, rows, key())
    assert counts(store) == (0, 0, 0, 0)


def test_concurrent_replay_across_store_instances(store):
    old = task(store)
    def submit(_):
        other = Store(store.path)
        return other.task_action(old['id'], 1, 'submit', {'evidence': '报告', 'acceptor': '甲'}, key())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(4)))
    assert all(r == results[0] for r in results)
    assert len(store.list_records('logs')) == 1
    assert len(store.history(old['id'])) == 2
