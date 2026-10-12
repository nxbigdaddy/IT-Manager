"""End-to-end: server-stored checklist templates and reminder rules reach the GUI."""
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from itmanager.gui import MainWindow
from itmanager.server import create_app

TOKEN = 'checklist-e2e-token-at-least-24'


class LocalClient:
    url = 'http://127.0.0.1:18765'

    def __init__(self, client):
        self.client = client

    def request(self, method, path, body=None):
        response = self.client.request(method, path, json=body)
        if response.status_code >= 400:
            raise RuntimeError(response.text)
        return response.json()

    def get(self, path):
        return self.request('GET', path)

    def post(self, path, body=None):
        return self.request('POST', path, body)


def drain(window, qt, timeout=5000):
    loop = QEventLoop()

    def check():
        if not window.runner.jobs:
            loop.quit()
        else:
            QTimer.singleShot(10, check)

    QTimer.singleShot(10, check)
    QTimer.singleShot(timeout, loop.quit)
    loop.exec()


def build(tmp_path):
    qt = QApplication.instance() or QApplication([])
    app = create_app(tmp_path, TOKEN)
    http = TestClient(app, headers={'Authorization': 'Bearer ' + TOKEN})
    window = MainWindow(LocalClient(http), autoload=False)
    errors = []
    window.error = lambda text: errors.append(str(text))
    return qt, app, http, window, errors


def test_custom_template_from_server_appears_in_checklist_dropdown(tmp_path):
    qt, app, http, window, errors = build(tmp_path)
    with http:
        templates = http.get('/checklist-templates').json()
        custom = {key: value for key, value in templates[0].items()
                  if key not in ('id', 'version', 'oldhistory', 'created', 'updated')}
        custom['name'] = '自定义检查模板'
        custom['items'] = [{'name': '自定义检查项一', 'hint': '通过判定：留痕', 'required': True}]
        saved = http.post('/checklist-templates', json={'data': custom})
        assert saved.status_code == 200, saved.text
        window.refresh()
        drain(window, qt)
        window.navigate(window.keys.index('checklists'))
        drain(window, qt)
        names = [window.checklist_template.itemText(i) for i in range(window.checklist_template.count())]
        assert '自定义检查模板' in names, names
        assert '入离职权限' in names  # untouched built-in templates stay available
        assert errors == []


def test_server_reminder_rules_drive_dashboard_banner(tmp_path):
    qt, app, http, window, errors = build(tmp_path)
    with http:
        soon = (date.today() + timedelta(days=40)).isoformat()
        http.post('/records/contracts', json={'data': {'名称': '演示合同', '到期日期': soon}})
        # Default contract rule warns 60 days ahead, so this record must be reported.
        window.refresh()
        drain(window, qt)
        window.navigate(window.keys.index('reminders'))
        drain(window, qt)
        contract_rule = next(rule for rule in window._reminders_rules if rule['key'] == 'contract_expiry')
        tightened = dict(contract_rule, warn_days=10)
        response = http.post('/reminders', json={'id': contract_rule['id'], 'version': contract_rule['version'],
                                                'data': tightened})
        assert response.status_code == 200, response.text
        window.navigate(window.keys.index('reminders'))
        drain(window, qt)
        window.navigate(window.keys.index('dashboard'))
        drain(window, qt)
        banners = [b.text() for b in window.page.findChildren(QPushButton) if '待处理提醒' in b.text()]
        assert banners, 'dashboard banner missing'
        assert '待处理提醒 0 项' in banners[-1], banners[-1]
        assert errors == []


def test_checklist_submit_creates_risk_and_rework_task_through_gui(tmp_path):
    qt, app, http, window, errors = build(tmp_path)
    with http:
        window.refresh()
        drain(window, qt)
        window.navigate(window.keys.index('checklists'))
        drain(window, qt)
        window.checklist_owner.setText('虚构检查负责人')
        rows = window.checklist_items.rowCount()
        assert rows >= 3
        for row in range(rows):
            window.checklist_items.cellWidget(row, 1).setCurrentText('通过')
        window.checklist_items.cellWidget(0, 1).setCurrentText('异常')
        window.checklist_items.cellWidget(0, 2).setText('最近一次备份作业失败，未找到恢复验证记录')
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
            window._checklist_submit()
        drain(window, qt)
        records = http.get('/records').json()
        risks = [r for r in records if r['module'] == 'risks']
        tasks = [r for r in records if r['module'] == 'tasks']
        assert len(risks) == 1, risks
        assert len(tasks) == 1, tasks
        assert '未找到恢复验证记录' in json.dumps(risks[0]['data'], ensure_ascii=False)
        run = http.get('/checklists/runs').json()
        assert len(run) == 1
        assert run[0]['items'][0]['result'] == '异常'
        notice = window.checklist_message.text()
        assert '任务 IDs' in notice and tasks[0]['id'] in notice
        assert not window.checklist_submit.isEnabled()
        assert errors == []
