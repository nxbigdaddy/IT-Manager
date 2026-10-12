"""Global search + optional tray controller + application version consistency."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date, timedelta
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from itmanager import __version__
from itmanager.gui import MainWindow
from itmanager.reminders import default_rules


class Client:
    url = 'http://127.0.0.1:18765'

    def get(self, path):
        if path == '/health':
            return {'status': 'ok', 'version': __version__, 'scheduler': True,
                    'scheduler_status': {'last_tick': None, 'error': None}}
        if path == '/reminders':
            return [dict(rule, id='R-' + rule['key'], version=1) for rule in default_rules()]
        if path == '/reminders/states':
            return []
        return []

    def post(self, path, payload=None):
        return {}


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


def build(qapp):
    window = MainWindow(Client(), autoload=False)
    window.records = [
        {'id': 'TASK-1', 'module': 'tasks', 'version': 1, 'archived': 0,
         'updated': '2026-10-10T08:00:00Z',
         'data': {'名称': '备份演练任务', '负责人': '张工', '状态': '进行中',
                  '截止日期': (date.today() - timedelta(days=1)).isoformat()}},
        {'id': 'SYS-1', 'module': 'systems', 'version': 1, 'archived': 0,
         'updated': '2026-10-10T08:00:00Z',
         'data': {'名称': '备份系统', '技术负责人': '李工'}},
    ]
    return window


def test_version_is_single_source_and_gui_shows_server_version(qapp):
    from itmanager.server import create_app
    from fastapi.testclient import TestClient
    import tempfile
    app = create_app(tempfile.mkdtemp(), 'v' * 32)
    with TestClient(app, headers={'Authorization': 'Bearer ' + 'v' * 32}) as c:
        assert c.get('/health').json()['version'] == __version__
    window = build(qapp)
    # Exercise the refresh callback synchronously so the connection label is asserted.
    class Runner:
        jobs = {}
        def submit(self, work, done, failed): done(work())
    window.runner = Runner()
    window.refresh()
    assert f'v{__version__}' in window.connection.text()
    assert '客户端 v' in window.connection.toolTip()


def test_version_mismatch_is_prominent(qapp):
    class OldClient(Client):
        def get(self, path):
            if path == '/health':return {'status':'ok','version':'0.4.0','scheduler':False}
            return []
    window = MainWindow(OldClient(), autoload=False)
    class Runner:
        jobs = {}
        def submit(self, work, done, failed): done(work())
    window.runner = Runner(); window.refresh()
    assert '版本不一致' in window.connection.text()
    assert '同时重启' in window.statusBar().currentMessage()


def test_tray_items_are_active_and_do_not_include_evidence(qapp):
    window = build(qapp)
    items = window.tray_items()
    assert items and items[0]['record_id'] == 'TASK-1'
    assert all('证据' not in str(item) for item in items)


def test_tray_unavailable_degrades_without_timer(qapp):
    with patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=False):
        window = MainWindow(Client(), autoload=False)
    assert window.tray.available is False
    assert window.tray.start(window.tray_items) is False
    window.configure_tray_notifications()
    assert '不支持系统托盘' in window.statusBar().currentMessage()


def test_tray_default_disabled_and_close_stops_it(qapp):
    from PySide6.QtCore import QSettings
    settings = QSettings(QSettings.IniFormat, QSettings.UserScope, 'ITManagementTest', 'TrayHost')
    settings.clear(); settings.setValue('tray/enabled', False)
    with patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=True), \
            patch('itmanager.gui.TrayController') as controller_class:
        controller = controller_class.return_value
        controller.available = True
        controller.start.return_value = False
        window = MainWindow(Client(), autoload=False)
        controller.start.assert_called_once()
        window.close()
        controller.stop.assert_called_once()
