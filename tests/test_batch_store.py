"""Batch update/archive: atomicity, CAS and idempotency of the store layer."""
import pytest
from itmanager.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / 'batch.sqlite3'))


def make(store, module, data):
    return store.save_record(module, data)


def test_batch_update_applies_all_or_nothing(store):
    first = make(store, 'tasks', {'名称': '任务甲', '负责人': '甲', '状态': '未开始'})
    second = make(store, 'tasks', {'名称': '任务乙', '负责人': '乙', '状态': '未开始'})
    updates = [{'id': first['id'], 'version': first['version'], 'data': dict(first['data'], 负责人='丙')},
               {'id': second['id'], 'version': second['version'], 'data': dict(second['data'], 负责人='丙')}]
    result = store.batch_update_records(updates, 'batch-update-request-001')
    assert result['count'] == 2
    assert {row['version'] for row in result['records']} == {2}
    saved = {r['id']: r for r in store.list_records()}
    assert saved[first['id']]['data']['负责人'] == '丙'
    assert saved[second['id']]['data']['负责人'] == '丙'
    assert len(store.record_history(first['id'])) == 2


def test_batch_update_rejects_stale_version_without_partial_write(store):
    first = make(store, 'tasks', {'名称': '任务甲', '状态': '未开始'})
    second = make(store, 'tasks', {'名称': '任务乙', '状态': '未开始'})
    updates = [{'id': first['id'], 'version': first['version'], 'data': dict(first['data'], 状态='进行中')},
               {'id': second['id'], 'version': 99, 'data': dict(second['data'], 状态='进行中')}]
    with pytest.raises(ValueError, match='记录已被更新'):
        store.batch_update_records(updates, 'batch-update-request-002')
    saved = {r['id']: r for r in store.list_records()}
    assert saved[first['id']]['data']['状态'] == '未开始'  # the first item must roll back too
    assert saved[first['id']]['version'] == 1


def test_batch_update_validation_failure_rolls_back_everything(store):
    first = make(store, 'tasks', {'名称': '任务甲', '状态': '未开始'})
    second = make(store, 'tasks', {'名称': '任务乙', '状态': '未开始'})
    updates = [{'id': first['id'], 'version': first['version'], 'data': dict(first['data'], 状态='进行中')},
               {'id': second['id'], 'version': second['version'],
                'data': dict(second['data'], 状态='已完成')}]  # completion needs evidence+acceptor
    with pytest.raises(ValueError):
        store.batch_update_records(updates, 'batch-update-request-003')
    saved = {r['id']: r for r in store.list_records()}
    assert saved[first['id']]['data']['状态'] == '未开始'
    assert saved[second['id']]['version'] == 1


def test_batch_update_is_idempotent_by_request_id(store):
    first = make(store, 'tasks', {'名称': '任务甲', '状态': '未开始'})
    updates = [{'id': first['id'], 'version': first['version'], 'data': dict(first['data'], 状态='进行中')}]
    once = store.batch_update_records(updates, 'batch-update-request-004')
    replay = store.batch_update_records(updates, 'batch-update-request-004')
    assert replay == once
    assert store.list_records()[0]['version'] == 2
    changed = [{'id': first['id'], 'version': first['version'], 'data': dict(first['data'], 状态='阻塞')}]
    with pytest.raises(ValueError, match='幂等请求ID已用于不同操作'):
        store.batch_update_records(changed, 'batch-update-request-004')


def test_batch_update_rejects_bad_shapes_and_duplicates(store):
    record = make(store, 'tasks', {'名称': '任务甲'})
    with pytest.raises(ValueError):
        store.batch_update_records([], 'batch-update-request-006')
    with pytest.raises(ValueError):
        store.batch_update_records([{'id': record['id'], 'data': {}}], 'batch-update-request-007')
    with pytest.raises(ValueError, match='重复记录'):
        store.batch_update_records([{'id': record['id'], 'version': 1, 'data': record['data']},
                                    {'id': record['id'], 'version': 1, 'data': record['data']}],
                                   'batch-update-request-008')
    with pytest.raises(ValueError, match='幂等请求ID'):
        store.batch_update_records([{'id': record['id'], 'version': 1, 'data': record['data']}], 'short')


def test_batch_archive_refuses_referenced_records(store):
    project = make(store, 'projects', {'名称': '演示项目'})
    task = make(store, 'tasks', {'名称': '演示任务', '项目ID': project['id']})
    with pytest.raises(ValueError, match='仍被有效记录引用'):
        store.batch_archive_records([{'id': project['id'], 'version': project['version']}],
                                    'batch-archive-request-001')
    assert all(not r['archived'] for r in store.list_records())
    result = store.batch_archive_records([{'id': task['id'], 'version': task['version']}],
                                        'batch-archive-request-002')
    assert result['count'] == 1
    assert store.list_records()[0]['id'] == project['id']


def test_batch_archive_is_atomic_and_idempotent(store):
    first = make(store, 'tasks', {'名称': '任务甲'})
    second = make(store, 'tasks', {'名称': '任务乙'})
    targets = [{'id': first['id'], 'version': first['version']}, {'id': second['id'], 'version': 99}]
    with pytest.raises(ValueError, match='记录已被更新'):
        store.batch_archive_records(targets, 'batch-archive-request-003')
    assert all(not r['archived'] for r in store.list_records())
    good = [{'id': first['id'], 'version': first['version']}, {'id': second['id'], 'version': second['version']}]
    done = store.batch_archive_records(good, 'batch-archive-request-004')
    assert done['count'] == 2
    assert store.batch_archive_records(good, 'batch-archive-request-004') == done
    assert all(r['archived'] for r in store.list_records())
