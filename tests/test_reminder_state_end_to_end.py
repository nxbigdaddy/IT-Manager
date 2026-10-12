"""End-to-end: acknowledging, snoozing and ignoring reminders through the page."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QInputDialog

from itmanager.gui import MainWindow
from itmanager.server import create_app

TOKEN = 'reminder-state-token-at-least-24'


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


def seed_contract(http, days):
    return http.post('/records/contracts', json={
        'data': {'名称': '演示合同', '到期日期': (date.today() + timedelta(days=days)).isoformat()}}).json()


def open_reminders(window, qt):
    window.refresh()
    drain(window, qt)
    window.navigate(window.keys.index('reminders'))
    drain(window, qt)
    drain(window, qt)
    return window.reminders_table


def test_mark_done_hides_reminder_and_survives_reload(tmp_path):
    qt, app, http, window, errors = build(tmp_path)
    with http:
        record = seed_contract(http, 10)
        table = open_reminders(window, qt)
        assert table.rowCount() == 1, table.rowCount()
        table.selectRow(0)
        window._reminders_set_state('done')
        drain(window, qt)
        assert table.rowCount() == 0
        assert errors == []
        stored = http.get('/reminders/states').json()
        assert len(stored) == 1
        assert stored[0]['action'] == 'done'
        assert stored[0]['record_id'] == record['id']
        assert stored[0]['record_version'] == record['version']
        # A handled reminder is still auditable when the operator asks for it.
        window.reminders_show_handled.setChecked(True)
        assert table.rowCount() == 1


def test_record_change_reopens_a_handled_reminder(tmp_path):
    qt, app, http, window, errors = build(tmp_path)
    with http:
        record = seed_contract(http, 10)
        table = open_reminders(window, qt)
        table.selectRow(0)
        window._reminders_set_state('done')
        drain(window, qt)
        assert table.rowCount() == 0
        http.post('/records/contracts', json={'id': record['id'], 'version': record['version'],
                                              'data': dict(record['data'], 名称='演示合同（已更新）')})
        window.refresh()
        drain(window, qt)
        window.navigate(window.keys.index('reminders'))
        drain(window, qt)
        drain(window, qt)
        assert window.reminders_table.rowCount() == 1, 'an updated record must be reported again'
        assert errors == []


def test_snooze_until_future_hides_and_past_snooze_reappears(tmp_path):
    qt, app, http, window, errors = build(tmp_path)
    with http:
        record = seed_contract(http, 10)
        table = open_reminders(window, qt)
        table.selectRow(0)
        with patch.object(QInputDialog, 'getText', return_value=((date.today() + timedelta(days=5)).isoformat(), True)):
            window._reminders_set_state('snooze')
        drain(window, qt)
        assert table.rowCount() == 0
        stored = http.get('/reminders/states').json()[0]
        assert stored['action'] == 'snooze' and stored['until']
        # Simulate an expired snooze at the storage layer: the page must show it again.
        expired = {key: value for key, value in stored.items() if key != 'id'}
        expired['until'] = (date.today() - timedelta(days=1)).isoformat()
        app.state.store.put('reminder_states', expired, stored['id'])
        window.navigate(window.keys.index('reminders'))
        drain(window, qt)
        drain(window, qt)
        assert window.reminders_table.rowCount() == 1
        assert errors == []


def test_ignore_requires_reason_and_blocks_bad_input(tmp_path):
    qt, app, http, window, errors = build(tmp_path)
    with http:
        seed_contract(http, 10)
        table = open_reminders(window, qt)
        table.selectRow(0)
        with patch.object(QInputDialog, 'getText', return_value=('', True)):
            window._reminders_set_state('dismiss')
        drain(window, qt)
        assert http.get('/reminders/states').json() == []
        assert '必须填写原因' in window.reminders_message.text()
        assert table.rowCount() == 1
        with patch.object(QInputDialog, 'getText', return_value=('重复项，已由周检覆盖', True)):
            window._reminders_set_state('dismiss')
        drain(window, qt)
        assert table.rowCount() == 0
        assert http.get('/reminders/states').json()[0]['reason'] == '重复项，由周检覆盖'.replace('由周检覆盖', '已由周检覆盖')
        assert errors == []
