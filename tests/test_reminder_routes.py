"""Server routes for reminder rules and configurable checklist templates."""
from datetime import date, timedelta
from fastapi.testclient import TestClient
from itmanager.server import create_app
from itmanager.schema import MODULES


def client_for(tmp_path):
    app = create_app(tmp_path, 'k' * 32)
    return app, TestClient(app, headers={'Authorization': 'Bearer ' + 'k' * 32})


def test_reminder_delete_removes_rule_with_version_cas(tmp_path):
    app, c = client_for(tmp_path)
    with c:
        rules = c.get('/reminders').json()
        target = rules[-1]
        stale = c.post('/reminders/delete', json={'id': target['id'], 'version': 99})
        assert stale.status_code == 400
        removed = c.post('/reminders/delete', json={'id': target['id'], 'version': target['version']})
        assert removed.status_code == 200, removed.text
        remaining = c.get('/reminders').json()
        assert target['id'] not in {rule['id'] for rule in remaining}
        assert len(remaining) == len(rules) - 1
        assert c.post('/reminders/delete', json={'id': target['id']}).status_code == 400
        # Deleting a built-in rule must not come back after a server restart.
        create_app(tmp_path, 'k' * 32)
        assert target['id'] not in {rule['id'] for rule in app.state.store.objects('reminders')}


def test_deleting_every_seeded_object_survives_restart(tmp_path):
    app, c = client_for(tmp_path)
    with c:
        for rule in c.get('/reminders').json():
            assert c.post('/reminders/delete', json={'id': rule['id'], 'version': rule['version']}).status_code == 200
        for template in c.get('/checklist-templates').json():
            response = c.post('/checklist-templates/delete',
                              json={'id': template['id'], 'version': template['version']})
            if response.status_code == 400:
                # Templates referenced by a finished run are protected; disable them instead.
                disabled = dict(template, enabled=False)
                disable = c.post('/checklist-templates', json={'id': template['id'], 'version': template['version'],
                                                              'data': {k: v for k, v in disabled.items()
                                                                       if k not in ('id', 'oldhistory', 'created',
                                                                                    'updated')}})
                assert disable.status_code == 200, disable.text
    restarted = create_app(tmp_path, 'k' * 32)
    assert restarted.state.store.objects('reminders') == []
    assert all(not t.get('enabled', True) for t in restarted.state.store.objects('checklist_templates'))


def test_reminder_rules_seeded_and_versioned(tmp_path):
    app, c = client_for(tmp_path)
    with c:
        rules = c.get('/reminders').json()
        assert len(rules) >= 6
        assert all(rule.get('version') == 1 for rule in rules)
        assert {rule['type'] for rule in rules} <= {'expiry', 'overdue', 'stale'}
        rule = rules[0]
        rule['warn_days'] = 45
        saved = c.post('/reminders', json={'id': rule['id'], 'version': rule['version'], 'data': rule})
        assert saved.status_code == 200, saved.text
        assert saved.json()['version'] == 2
        stale = c.post('/reminders', json={'id': rule['id'], 'version': 1, 'data': rule})
        assert stale.status_code == 400
        invalid = c.post('/reminders', json={'data': dict(rule, type='unknown')})
        assert invalid.status_code == 400
        assert invalid.json()['user_message']
        # Seeding is idempotent across restarts on the same data directory.
        create_app(tmp_path, 'k' * 32)
        assert len(app.state.store.objects('reminders')) == len(rules)


def test_checklist_templates_seeded_editable_and_export(tmp_path):
    app, c = client_for(tmp_path)
    with c:
        templates = c.get('/checklist-templates').json()
        assert len(templates) == 4
        names = {t['name'] for t in templates}
        assert {'备份运行', '入离职权限', '合同续费', '周度项目检查'} == names
        assert all(t['items'] for t in templates)
        template = templates[0]
        from itmanager.checklist_templates import GENERIC_SCOPE, scope_choices
        allowed = {key for key, _ in scope_choices()} | {GENERIC_SCOPE}
        assert all(part.strip() in allowed for part in str(template.get('scope', '')).split('、'))
        edited = dict(template)
        edited['items'] = list(template['items']) + [{'name': '新增演示检查项', 'hint': '说明通过标准', 'required': False}]
        saved = c.post('/checklist-templates', json={'id': edited['id'], 'version': edited['version'], 'data': edited})
        assert saved.status_code == 200, saved.text
        assert saved.json()['version'] == 2
        assert len(saved.json()['items']) == len(template['items']) + 1
        conflict = c.post('/checklist-templates', json={'id': edited['id'], 'version': 1, 'data': edited})
        assert conflict.status_code == 400
        exported = c.get('/checklist-templates/' + edited['id'] + '/export').json()
        assert set(exported) == {'name', 'description', 'scope', 'items', 'enabled'}
        assert 'id' not in exported and 'version' not in exported
        assert c.get('/checklist-templates/missing/export').status_code == 400
        bad = c.post('/checklist-templates', json={'data': dict(edited, items=[])})
        assert bad.status_code == 400
        assert bad.json()['user_message']


def test_checklist_template_delete_guard_and_version(tmp_path):
    app, c = client_for(tmp_path)
    with c:
        templates = c.get('/checklist-templates').json()
        fresh = {key: value for key, value in templates[1].items()
                 if key not in ('id', 'version', 'oldhistory', 'created', 'updated')}
        fresh['name'] = '待删除模板'
        created = c.post('/checklist-templates', json={'data': fresh}).json()
        stale = c.post('/checklist-templates/delete', json={'id': created['id'], 'version': 99})
        assert stale.status_code == 400
        removed = c.post('/checklist-templates/delete', json={'id': created['id'], 'version': created['version']})
        assert removed.status_code == 200, removed.text
        assert removed.json()['deleted'] is True
        assert created['id'] not in {t['id'] for t in c.get('/checklist-templates').json()}
        assert c.post('/checklist-templates/delete', json={'id': created['id']}).status_code == 400
        # A template referenced by a finished checklist run must be disabled, not deleted.
        used = c.get('/checklist-templates').json()[0]
        run = c.post('/checklists/complete', json={
            'name': used['name'], 'date': '2030-01-02', 'owner': '虚构检查人',
            'items': [{'name': '示例检查项', 'result': '通过', 'note': ''}],
            'request_id': 'template_delete_guard_001'})
        assert run.status_code == 200, run.text
        blocked = c.post('/checklist-templates/delete', json={'id': used['id'], 'version': used['version']})
        assert blocked.status_code == 400
        assert '改为停用' in blocked.text
        assert used['id'] in {t['id'] for t in c.get('/checklist-templates').json()}


def test_dashboard_reminder_preview_uses_real_records(tmp_path):
    from itmanager.reminders import compute_items, default_rules
    app, c = client_for(tmp_path)
    with c:
        soon = (date.today() + timedelta(days=5)).isoformat()
        saved = c.post('/records/contracts', json={'data': {'名称': '演示合同', '到期日期': soon}}).json()
        assert saved['id']
        records = c.get('/records').json()
        items = compute_items(records, [r for r in default_rules() if r.get('enabled', True)], date.today())
        assert any(item['record_id'] == saved['id'] for item in items)
