"""Batch update/archive HTTP contract on the real service."""
from fastapi.testclient import TestClient
from itmanager.server import create_app


def client_for(tmp_path):
    app = create_app(tmp_path, 'b' * 32)
    return app, TestClient(app, headers={'Authorization': 'Bearer ' + 'b' * 32})


def test_batch_route_updates_and_replays(tmp_path):
    app, c = client_for(tmp_path)
    with c:
        first = c.post('/records/tasks', json={'data': {'名称': '任务甲', '状态': '未开始'}}).json()
        second = c.post('/records/tasks', json={'data': {'名称': '任务乙', '状态': '未开始'}}).json()
        payload = {'updates': [{'id': first['id'], 'version': first['version'],
                                'data': dict(first['data'], 负责人='批量负责人')},
                               {'id': second['id'], 'version': second['version'],
                                'data': dict(second['data'], 负责人='批量负责人')}],
                   'request_id': 'batch-route-request-001'}
        response = c.post('/records/batch', json=payload)
        assert response.status_code == 200, response.text
        assert response.json()['count'] == 2
        assert c.post('/records/batch', json=payload).json() == response.json()
        records = {r['id']: r for r in c.get('/records').json()}
        assert records[first['id']]['data']['负责人'] == '批量负责人'
        assert records[first['id']]['version'] == 2
        bad = c.post('/records/batch', json={'updates': [], 'request_id': 'batch-route-request-002'})
        assert bad.status_code == 400
        assert bad.json()['user_message']


def test_batch_archive_route_blocks_referenced_records(tmp_path):
    app, c = client_for(tmp_path)
    with c:
        project = c.post('/records/projects', json={'data': {'名称': '演示项目'}}).json()
        task = c.post('/records/tasks', json={'data': {'名称': '演示任务', '项目ID': project['id']}}).json()
        blocked = c.post('/records/batch-archive', json={
            'targets': [{'id': project['id'], 'version': project['version']}],
            'request_id': 'batch-archive-route-001'})
        assert blocked.status_code == 400
        assert blocked.json()['user_message'] == '记录仍被有效记录引用，不能归档'
        done = c.post('/records/batch-archive', json={
            'targets': [{'id': task['id'], 'version': task['version']}],
            'request_id': 'batch-archive-route-002'})
        assert done.status_code == 200, done.text
        assert done.json()['count'] == 1
        assert c.get('/records').json()[0]['archived'] == 0
