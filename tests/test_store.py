"""Storage tests use only isolated temporary SQLite databases."""
import json
import sqlite3
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from itmanager.store import Store, SCHEMA_VERSION
from itmanager.repository import Repository, create_repository


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'test.sqlite3')


def record(oid, module='tasks', **data):
    return {'id': oid, 'module': module, 'data': {'名称': oid, **data}, 'version': 1}


def test_minimal_and_history(store):
    row = store.save_record('tasks', {'名称': '测试'})
    changed = store.save_record('tasks', {'名称': '修改'}, row['id'], 1)
    assert changed['version'] == 2
    assert [r['data']['名称'] for r in store.record_history(row['id'])] == ['测试', '修改']
    with pytest.raises(ValueError, match='刷新'):
        store.save_record('tasks', {'名称': '冲突'}, row['id'], 1)
    assert len(store.audit_rows()) == 2


@pytest.mark.parametrize('module,data', [
    ('tasks', {'名称': ''}), ('tasks', {'名称': 'x', '未知': 'a'}),
    ('tasks', {'名称': 'x', '开始日期': '2025-2-01'}),
    ('tasks', {'名称': 'x', '截止日期': '2025-02-30'}),
    ('tasks', {'名称': 'x', '开始日期': '2025-03-01', '截止日期': '2025-02-01'}),
    ('tasks', {'名称': 'x', '状态': '已完成'}),
    ('tasks', {'名称': 'x', '状态': '已完成', '完成证据': 'x', '验收人': ' '}),
    ('reviews', {'名称': 'x', '人员ID': 'missing'}),
    ('incidents', {'名称': 'x', '受理时间': '2025-02-30 09:00'}),
    ('systems', {'名称': 'x', 'RTO小时': 'NaN'}),
    ('contracts', {'名称': 'x', '金额': '-0.01'}),
    ('contracts', {'名称': 'x', '金额': 'Infinity'}),
    ('contracts', {'名称': 'x', '金额': '1e3'}),
    ('contracts', {'名称': 'x', '金额': 1.0}),
])
def test_invalid_business(store, module, data):
    with pytest.raises(ValueError):
        store.save_record(module, data)
    assert not store.list_records()
    assert not store.audit_rows()


def test_decimal_and_optional_fields(store):
    for value in ('0', '12345678901234567890.123456789', '', '0.00'):
        assert store.save_record('contracts', {'名称': '合同', '金额': value})['data']['金额'] == value
    person = store.save_record('people', {'姓名': '甲'})
    store.save_record('reviews', {'名称': '考核', '人员ID': person['id']})
    store.save_record('reviews', {'名称': '未采集'})
    store.save_record('tasks', {'名称': '交付', '状态': '已完成', '完成证据': '验收单', '验收人': '甲'})


def test_task_cycles_and_projects(store):
    project = store.save_record('projects', {'名称': '项目'})
    p = store.save_record('tasks', {'名称': '父', '项目ID': project['id']})
    child = store.save_record('tasks', {'名称': '子', '父任务ID': p['id'], '项目ID': project['id']})
    with pytest.raises(ValueError, match='环'):
        store.save_record('tasks', {**p['data'], '父任务ID': child['id']}, p['id'], 1)
    with pytest.raises(ValueError, match='同一项目'):
        store.save_record('tasks', {'名称': '父'}, p['id'], 1)
    with pytest.raises(ValueError, match='同一项目'):
        store.save_record('tasks', {'名称': '子2', '父任务ID': p['id']})
    with pytest.raises(ValueError, match='模块'):
        store.save_record('reviews', {'名称': '考核', '人员ID': p['id']})


def test_import_forward_refs_and_duplicates(store):
    rows = [record('child', 父任务ID='parent', 项目ID='project'),
            record('parent', 项目ID='project'), record('project', 'projects')]
    assert store.import_preview(rows) == {'valid': True, 'count': 3, 'errors': []}
    assert store.list_records() == []
    assert store.import_records(rows) == {'imported': 3}
    assert len(store.record_history('child')) == 1
    assert not store.import_preview(rows)['valid']
    with pytest.raises(ValueError, match='重复ID'):
        store.import_records(rows)
    assert len(store.list_records()) == 3


@pytest.mark.parametrize('rows', [
    [record('a', 父任务ID='b'), record('b', 父任务ID='a')],
    [record('a'), record('a')],
    [record('a'), record('b', 项目ID='missing')],
    [record('a', 父任务ID='a')],
    [record('a'), {**record('b'), 'version': True}],
    [record('a', 父任务ID='b'), record('b', 父任务ID=[])],
])
def test_import_all_or_nothing(store, rows):
    assert not store.import_preview(rows)['valid']
    with pytest.raises(ValueError):
        store.import_records(rows)
    assert not store.list_records() and not store.audit_rows()


def test_export_import_metadata(store, tmp_path):
    row = store.save_record('plans', {'名称': '长期'})
    other = Store(tmp_path / 'other.sqlite3')
    other.import_records(store.list_records())
    assert other.list_records() == [row]


def test_audit_failure_rolls_back(store, monkeypatch):
    def broken(*args):
        raise RuntimeError('audit failed')
    monkeypatch.setattr(store, 'audit', broken)
    with pytest.raises(RuntimeError):
        store.save_record('tasks', {'名称': 'x'})
    with pytest.raises(RuntimeError):
        store.import_records([record('a')])
    assert not store.list_records()
    assert not store.record_history('a')


def test_archive_reference_and_history(store):
    p = store.save_record('projects', {'名称': '项目'})
    task = store.save_record('tasks', {'名称': '任务', '项目ID': p['id']})
    with pytest.raises(ValueError, match='引用'):
        store.archive_record(p['id'], 1)
    assert store.archive_record(task['id'], 1)['archived'] == 1
    store.archive_record(p['id'], 1)
    assert not store.list_records()
    assert len(store.list_records(include_archived=True)) == 2
    assert store.record_history(task['id'])[-1]['version'] == 2
    with pytest.raises(ValueError):
        store.save_record('tasks', {'名称': '新', '项目ID': p['id']})
    with pytest.raises(ValueError, match='重复ID'):
        store.import_records([record(task['id'])])


def test_objects_delivery_compatibility(store):
    obj = store.put('models', {'name': 'x'})
    assert store.put('models', {**obj, 'name': 'y'}, obj['id'])['name'] == 'y'
    with pytest.raises(ValueError, match='ID'):
        store.put('models', {'id': 'other'}, obj['id'])
    delivery = store.claim_delivery('s', 'slot', {})
    assert delivery and store.claim_delivery('s', 'slot', {}) is None
    store.delivery_state(delivery, 'sent', {'ok': True})
    assert store.deliveries()[0]['state'] == 'sent'


def test_cross_instance_optimistic_lock(store):
    row = store.save_record('tasks', {'名称': 'x'})
    other = Store(store.path)
    def update(s):
        try:
            s.save_record('tasks', {'名称': 'new'}, row['id'], 1)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, [store, other])) == [False, True]
    assert len(store.record_history(row['id'])) == 2


def test_prototype_migration(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE records(id TEXT PRIMARY KEY,module TEXT NOT NULL,data TEXT NOT NULL,version INTEGER NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL)')
        db.execute('INSERT INTO records VALUES(?,?,?,?,?,?)', ('old', 'tasks', json.dumps({'名称': '旧', '负责人': ''}), 4, '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00'))
    migrated = Store(path)
    assert migrated.list_records()[0]['version'] == 4
    assert migrated.record_history('old')[0]['version'] == 4
    assert len(list(tmp_path.glob('*.bak'))) == 1
    Store(path)
    assert len(list(tmp_path.glob('*.bak'))) == 1
    with migrated.connect() as db:
        assert db.execute('SELECT MAX(version) FROM schema_versions').fetchone()[0] == SCHEMA_VERSION
        db.execute('INSERT INTO schema_versions VALUES(999,?)', ('future',))
    with pytest.raises(ValueError, match='版本'):
        Store(path)


def test_backup_offline_validation(store, tmp_path):
    row = store.save_record('tasks', {'名称': '备份'})
    store.archive_record(row['id'], 1)
    target = tmp_path / store.backup(tmp_path)
    assert Store.validate_backup(target) == {'valid': True, 'schema_version': SCHEMA_VERSION, 'records': 1, 'offline_only': True}
    # Offline operator workflow: validate, copy to a new directory, reopen.
    destination = tmp_path / 'restored' / 'itmanager.sqlite3'
    destination.parent.mkdir()
    shutil.copy2(target, destination)
    restored = Store(destination)
    assert restored.list_records(include_archived=True) == store.list_records(include_archived=True)
    assert restored.record_history(row['id']) == store.record_history(row['id'])
    assert restored.audit_rows() == store.audit_rows()
    bad = tmp_path / 'bad.sqlite3'
    bad.write_bytes(b'not sqlite')
    with pytest.raises(ValueError):
        Store.validate_backup(bad)
    with pytest.raises(ValueError):
        Store.validate_backup(tmp_path / 'missing')
    assert not (tmp_path / 'missing').exists()


def test_factory(tmp_path, monkeypatch):
    path = tmp_path / 'factory.sqlite3'
    assert isinstance(create_repository({'backend': 'sqlite', 'path': path}), Repository)
    for backend in ('postgres', 'postgresql', 'mysql', 'unknown'):
        with pytest.raises(ValueError, match='backend'):
            create_repository({'backend': backend, 'path': path})
    monkeypatch.setenv('ITMANAGER_DB_BACKEND', 'sqlite')
    monkeypatch.setenv('ITMANAGER_DB_PATH', str(path))
    assert isinstance(create_repository(), Store)


@pytest.mark.parametrize('score', ['0', '100', '99.999', ''])
def test_review_score_bounds(store, score):
    store.save_record('reviews', {'名称': '考核', '评分': score})


@pytest.mark.parametrize('data', [
    {'评分': '100.001'}, {'评分': '-0.01'}, {'状态': '未知'},
    {'状态': '已定稿'},
])
def test_review_invalid(store, data):
    with pytest.raises(ValueError):
        store.save_record('reviews', {'名称': '考核', **data})


def final_review(store):
    person = store.save_record('people', {'姓名': '员工'})
    return {'名称': '年度考核', '人员ID': person['id'], '考核周期': '2025',
            '成果证据': '交付清单', '主管评价': '达标', '员工反馈': '已确认',
            '复核人': '主管', '状态': '已定稿', '评分': '90'}


@pytest.mark.parametrize('missing', ['人员ID', '考核周期', '成果证据', '主管评价', '员工反馈', '复核人'])
def test_review_finalize_required(store, missing):
    data = final_review(store)
    data[missing] = ''
    with pytest.raises(ValueError, match='定稿缺少'):
        store.save_record('reviews', data)


def test_review_reopen_history(store):
    data = final_review(store)
    row = store.save_record('reviews', data)
    with pytest.raises(ValueError, match='待复核'):
        store.save_record('reviews', {**data, '评分': '95'}, row['id'], 1)
    with pytest.raises(ValueError, match='待复核'):
        store.save_record('reviews', {**data, '评分': '95', '状态': '待复核'}, row['id'], 1)
    reopened = store.save_record('reviews', {**data, '状态': '待复核'}, row['id'], 1)
    store.save_record('reviews', {**data, '评分': '95'}, row['id'], reopened['version'])
    history = store.record_history(row['id'])
    assert [r['data']['状态'] for r in history] == ['已定稿', '待复核', '已定稿']
    assert [r['data']['评分'] for r in history] == ['90', '90', '95']


@pytest.mark.parametrize('state', ['目标设定', '待评价', '待反馈', '待复核', ''])
def test_review_intermediate_optional(store, state):
    store.save_record('reviews', {'名称': '考核', '状态': state})


def test_review_import_validation(store):
    assert not store.import_preview([record('review', 'reviews', 评分='101')])['valid']
    with pytest.raises(ValueError):
        store.import_records([record('review', 'reviews', 状态='已定稿')])
    assert store.list_records() == []


def test_standalone_audit(store):
    store.record_audit('导出', 'records', '数量:0')
    row = store.audit_rows()[0]
    assert (row['action'], row['object_id'], row['detail']) == ('导出', 'records', '数量:0')


def test_concurrent_delivery_claim(store):
    other = Store(store.path)
    barrier = threading.Barrier(2)
    def claim(repository):
        barrier.wait(timeout=10)
        return repository.claim_delivery('schedule', 'same-slot', {'snapshot': 'test'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [store, other]))
    assert sum(result is not None for result in results) == 1
    assert len(store.deliveries()) == 1
    assert len([r for r in store.audit_rows() if r['action'] == '创建发送任务']) == 1
    # A new instance after restart still cannot reclaim the same slot.
    assert Store(store.path).claim_delivery('schedule', 'same-slot', {}) is None


def draft_channel(store):
    draft = store.put('drafts', {'title': '标题', 'content': '正文', 'status': 'draft'})
    channel = store.put('channels', {'type': 'wecom'})
    return draft, channel


def test_draft_review_cas(store):
    draft, channel = draft_channel(store)
    with pytest.raises(ValueError):
        store.review_draft(draft['id'], None, None, True)
    approved = store.review_draft(draft['id'], 1, None, True)
    assert approved['revision'] == approved['approved_revision'] == 2
    with pytest.raises(ValueError, match='刷新'):
        store.review_draft(draft['id'], 1, '不同', False)
    saved = store.review_draft(draft['id'], 2, None, False)
    assert saved['revision'] == 3 and saved['approved_revision'] is None
    with pytest.raises(ValueError):
        store.claim_approved_delivery(draft['id'], channel['id'], 'request-123456789', 3)


def test_concurrent_draft_reviews(store):
    draft, _ = draft_channel(store)
    other = Store(store.path)
    barrier = threading.Barrier(2)
    def review(repository):
        barrier.wait(timeout=10)
        try:
            repository.review_draft(draft['id'], 1, None, True)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(review, [store, other])) == [False, True]
    assert store.get('drafts', draft['id'])['revision'] == 2


@pytest.mark.parametrize('enabled', [False, 0, 1, 'true', None])
def test_manual_channel_requires_strict_boolean(store, enabled):
    draft, channel = draft_channel(store)
    store.review_draft(draft['id'], 1, None, True)
    store.put('channels', {**channel, 'enabled': enabled}, channel['id'])
    with pytest.raises(ValueError, match='停用'):
        store.claim_approved_delivery(draft['id'], channel['id'], 'request-123456789', 2)
    assert not store.deliveries()


def test_claim_snapshot_and_revocation_order(store):
    draft, channel = draft_channel(store)
    store.review_draft(draft['id'], 1, None, True)
    claim = store.claim_approved_delivery(draft['id'], channel['id'], 'request-123456789', 2)
    assert claim['draft']['content'] == '正文'
    store.review_draft(draft['id'], 2, '新正文', False)
    assert claim['draft']['content'] == '正文'
    assert store.claim_approved_delivery(draft['id'], channel['id'], 'request-123456789', 2) is None
    with pytest.raises(ValueError):
        store.claim_approved_delivery(draft['id'], channel['id'], 'request-987654321', 3)
    store.delivery_state(claim['id'], 'sent', {'content': '不能覆盖', 'revision': 999, 'finished': 'now'})
    detail = store.deliveries()[0]['data']
    assert detail['content'] == detail['snapshot']['content'] == '正文'
    assert detail['revision'] == 2 and detail['finished'] == 'now'


def test_concurrent_revoke_and_claim(store):
    draft, channel = draft_channel(store)
    store.review_draft(draft['id'], 1, None, True)
    other = Store(store.path)
    barrier = threading.Barrier(2)
    def revoke():
        barrier.wait(timeout=10)
        return other.review_draft(draft['id'], 2, '撤销正文', False)
    def claim():
        barrier.wait(timeout=10)
        try:
            return store.claim_approved_delivery(draft['id'], channel['id'], 'request-123456789', 2)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(revoke), pool.submit(claim)
        assert a.result()['revision'] == 3
        result = b.result()
    assert store.get('drafts', draft['id'])['status'] == 'draft'
    if result:
        assert result['draft']['content'] == '正文'
        assert store.deliveries()[0]['data']['revision'] == 2
    else:
        assert not store.deliveries()


def test_manual_duplicate_concurrent_claim(store):
    draft, channel = draft_channel(store)
    store.review_draft(draft['id'], 1, None, True)
    other = Store(store.path)
    barrier = threading.Barrier(2)
    def claim(repository):
        barrier.wait(timeout=10)
        return repository.claim_approved_delivery(draft['id'], channel['id'], 'request-123456789', 2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [store, other]))
    assert sum(r is not None for r in results) == 1
    assert len(store.deliveries()) == 1
