"""AI weekly summary and minutes→tasks over HTTP, with the model mocked."""
import json
from unittest.mock import patch

from fastapi.testclient import TestClient
from itmanager.server import create_app


def client_for(tmp_path):
    app = create_app(tmp_path, 'w' * 32)
    return app, TestClient(app, headers={'Authorization': 'Bearer ' + 'w' * 32})


def provider(c):
    return c.post('/objects/providers', json={'data': {
        'provider': 'deepseek', 'model': 'deepseek-chat', 'api_key_env': 'DEEPSEEK_API_KEY'}}).json()


def fake_generate(text):
    return {'text': text, 'model': 'mock-model', 'usage': {'total_tokens': 12}}


def test_weekly_summary_creates_reviewable_draft(tmp_path):
    app, c = client_for(tmp_path)
    with c:
        cfg = provider(c)
        c.post('/records/tasks', json={'data': {'名称': '本周完成任务', '状态': '已完成',
                                                '负责人': '甲', '截止日期': '2030-01-01'}})
        c.post('/records/risks', json={'data': {'名称': '演示风险', '等级': '高'}})
        payload = {'provider_id': cfg['id'], 'start': '2030-01-01', 'end': '2030-01-31',
                   'owner': '', 'confirmed': True}
        with patch('itmanager.ai.generate', return_value=fake_generate('## 本期完成\n- 演示任务')):
            response = c.post('/ai/weekly-summary', json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['draft']['status'] == 'draft'
        assert '2030-01-01' in body['draft']['content']
        assert '本期完成' in body['draft']['content']
        calls = c.get('/ai/calls').json()
        assert calls and calls[-1]['purpose'] == 'weekly_summary'
        # The draft still needs explicit human approval before it can be sent.
        assert c.post('/send', json={'draft_id': body['draft']['id'], 'channel_id': 'none',
                                     'request_id': 'weekly-summary-send-01'}).status_code in (400, 500)


def test_weekly_summary_requires_authorisation_for_sensitive_scope(tmp_path):
    app, c = client_for(tmp_path)
    with c:
        cfg = provider(c)
        c.post('/records/people', json={'data': {'姓名': '虚构员工'}})
        base = {'provider_id': cfg['id'], 'start': '2000-01-01', 'end': '2100-01-01', 'confirmed': True}
        with patch('itmanager.ai.generate', return_value=fake_generate('正文')):
            denied = c.post('/ai/weekly-summary', json=base)
            assert denied.status_code == 400
            assert '授权' in denied.text
            allowed = c.post('/ai/weekly-summary', json=dict(base, sensitive_confirmed=True))
        assert allowed.status_code == 200, allowed.text


def test_minutes_to_tasks_returns_candidates_and_supports_creation(tmp_path):
    app, c = client_for(tmp_path)
    with c:
        cfg = provider(c)
        person = c.post('/records/people', json={'data': {'姓名': '张工'}}).json()
        model_text = json.dumps({'tasks': [
            {'name': '完成备份演练', 'owner': '张工', 'due_date': '2030-02-01', 'priority': '高',
             'next_step': '预约窗口', 'acceptance': '演练记录与结论'},
            {'name': '无人负责的事项', 'owner': '不存在的人', 'due_date': '不是日期', 'priority': '紧急',
             'next_step': '', 'acceptance': ''}]}, ensure_ascii=False)
        with patch('itmanager.ai.generate', return_value=fake_generate(model_text)):
            response = c.post('/ai/minutes-tasks', json={'provider_id': cfg['id'], 'minutes': '会议纪要内容',
                                                        'confirmed': True})
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body['tasks']) == 2
        assert body['tasks'][0]['owner'] == '张工'
        second = body['tasks'][1]
        assert second['owner'] == '' and second['due_date'] == '' and second['priority'] == ''
        assert second['warnings']
        assert body['source_id'].startswith('ai:')
        # The confirmed candidates become real tasks through the existing batch endpoint.
        created = c.post('/actions/create', json={
            'source_id': body['source_id'],
            'items': [{'name': body['tasks'][0]['name'], 'owner': body['tasks'][0]['owner'],
                       'due_date': body['tasks'][0]['due_date'], 'next_step': '预约窗口',
                       'acceptance': '演练记录与结论'}],
            'request_id': 'minutes-tasks-create-01'})
        assert created.status_code == 200, created.text
        assert len(created.json()['tasks']) == 1
        assert c.post('/ai/minutes-tasks', json={'provider_id': cfg['id'], 'minutes': '   ',
                                                 'confirmed': True}).status_code == 400
        assert c.post('/ai/minutes-tasks', json={'provider_id': cfg['id'], 'minutes': '内容'}).status_code == 400
        assert person['id']


def test_minutes_parse_failure_is_reported_not_silently_empty(tmp_path):
    app, c = client_for(tmp_path)
    with c:
        cfg = provider(c)
        with patch('itmanager.ai.generate', return_value=fake_generate('这不是JSON')):
            response = c.post('/ai/minutes-tasks', json={'provider_id': cfg['id'], 'minutes': '纪要',
                                                         'confirmed': True})
        assert response.status_code == 400
        calls = c.get('/ai/calls').json()
        assert calls and calls[-1]['status'] == 'failed'
