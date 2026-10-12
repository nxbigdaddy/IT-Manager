"""Server-side filtering and paging for large ledgers."""
from fastapi.testclient import TestClient
from itmanager.server import create_app
from itmanager.store import Store


def make(store, module, data):
    return store.save_record(module, data)


def test_page_records_filters_before_slicing(tmp_path):
    store = Store(str(tmp_path / 'page.sqlite3'))
    for index in range(7):
        make(store, 'tasks', {'名称': f'任务{index}', '负责人': '甲' if index % 2 else '乙',
                              '状态': '进行中', '截止日期': f'2030-01-{index + 1:02d}'})
    page = store.page_records('tasks', limit=3, offset=0)
    assert page['total'] == 7 and len(page['records']) == 3 and page['has_more'] is True
    last = store.page_records('tasks', limit=3, offset=6)
    assert len(last['records']) == 1 and last['has_more'] is False
    by_owner = store.page_records('tasks', owner='甲')
    assert by_owner['total'] == 3
    by_status = store.page_records('tasks', status='进行中', limit=500)
    assert by_status['total'] == 7
    by_status = store.page_records('tasks', status='已完成')
    assert by_status['total'] == 0
    by_date = store.page_records('tasks', start='2030-01-03', end='2030-01-05')
    assert by_date['total'] == 3
    by_query = store.page_records('tasks', query='任务5')
    assert by_query['total'] == 1
    assert by_query['records'][0]['data']['名称'] == '任务5'
    # Archived rows stay out unless asked for.
    target = by_query['records'][0]
    store.archive_record(target['id'], target['version'])
    assert store.page_records('tasks', query='任务5')['total'] == 0
    assert store.page_records('tasks', query='任务5', include_archived=True)['total'] == 1


def test_page_records_validates_arguments(tmp_path):
    store = Store(str(tmp_path / 'page2.sqlite3'))
    make(store, 'tasks', {'名称': '任务'})
    for kwargs in ({'limit': 0}, {'limit': 501}, {'limit': 'x'}, {'offset': -1},
                   {'module': 'unknown'}, {'query': 5}, {'query': 'x' * 201}):
        try:
            store.page_records(**kwargs)
        except ValueError:
            continue
        raise AssertionError(f'expected ValueError for {kwargs}')


def test_page_route_returns_envelope_and_keeps_list_route(tmp_path):
    app = create_app(tmp_path, 'p' * 32)
    with TestClient(app, headers={'Authorization': 'Bearer ' + 'p' * 32}) as c:
        for index in range(5):
            c.post('/records/tasks', json={'data': {'名称': f'路由任务{index}', '负责人': '甲'}})
        page = c.get('/records/page', params={'module': 'tasks', 'limit': 2}).json()
        assert set(page) == {'records', 'total', 'limit', 'offset', 'has_more'}
        assert page['total'] == 5 and len(page['records']) == 2
        assert c.get('/records/page', params={'limit': 0}).status_code == 400
        # The original list route stays unchanged for callers that need everything.
        assert len(c.get('/records').json()) == 5
        assert len(c.get('/records', params={'module': 'tasks'}).json()) == 5
