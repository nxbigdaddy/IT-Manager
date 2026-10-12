"""End-to-end: resolving and ignoring health findings in the page."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from unittest.mock import patch

from fastapi.testclient import TestClient
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QInputDialog

from itmanager.gui import MainWindow
from itmanager.health import scan
from itmanager.server import create_app

TOKEN = 'health-state-token-at-least-24'


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


def open_health(window, qt):
    window.refresh()
    drain(window, qt)
    window.navigate(window.keys.index('health'))
    drain(window, qt)
    drain(window, qt)


def test_resolved_finding_hides_until_record_changes(tmp_path):
    qt, app, http, window, errors = build(tmp_path)
    with http:
        record = http.post('/records/tasks', json={
            'data': {'名称': '缺少验收依据的任务', '状态': '进行中'}}).json()
        open_health(window, qt)
        expected = len(scan(window.records)['findings'])
        assert expected >= 1
        assert window.health_table.rowCount() == expected
        window.health_table.selectRow(0)
        finding = window._health_selected_finding()
        assert finding and finding.get('code')
        window._health_set_state('resolved')
        drain(window, qt)
        assert window.health_table.rowCount() == expected - 1
        stored = http.get('/health/states').json()
        assert len(stored) == 1 and stored[0]['status'] == 'resolved'
        assert stored[0]['record_id'] == finding['record_id']
        assert stored[0]['record_version'] == record['version']
        # The operator can still review what was resolved.
        window.health_show_handled.setChecked(True)
        drain(window, qt)
        assert window.health_table.rowCount() == expected
        # Editing the record invalidates the resolution and the finding returns.
        http.post('/records/tasks', json={'id': record['id'], 'version': record['version'],
                                          'data': dict(record['data'], 名称='记录已修改')})
        window.health_show_handled.setChecked(False)
        window.refresh()
        drain(window, qt)
        window.navigate(window.keys.index('health'))
        drain(window, qt)
        drain(window, qt)
        assert window.health_table.rowCount() == expected
        assert errors == []


def test_ignore_requires_reason(tmp_path):
    qt, app, http, window, errors = build(tmp_path)
    with http:
        http.post('/records/tasks', json={'data': {'名称': '待复核任务', '状态': '进行中'}})
        open_health(window, qt)
        before = window.health_table.rowCount()
        assert before >= 1
        window.health_table.selectRow(0)
        with patch.object(QInputDialog, 'getText', return_value=('', True)):
            window._health_set_state('ignored')
        drain(window, qt)
        assert http.get('/health/states').json() == []
        assert '必须填写原因' in window.health_task_message.text()
        assert window.health_table.rowCount() == before
        with patch.object(QInputDialog, 'getText', return_value=('字段不适用于本模块', True)):
            window._health_set_state('ignored')
        drain(window, qt)
        assert window.health_table.rowCount() == before - 1
        assert http.get('/health/states').json()[0]['reason'] == '字段不适用于本模块'
        assert errors == []
