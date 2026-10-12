"""Offline regression contracts for UI async work (no network or real workers).

Run: python -m pytest -q tests/test_ui_async_v2.py
Failures intentionally expose current bugs; do not xfail them during the refactor.
The queued runner lets each test choose completion order independently of dispatch.
"""
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit
from itmanager import gui
from itmanager.client import ClientError


class MockRunner:
    """No threads: separate queued work execution from callback delivery."""
    def __init__(self):
        self.pending = []
        self.jobs = {}

    def submit(self, work, success, error):
        index = len(self.pending)
        self.pending.append((work, success, error))
        self.jobs[index] = True
        return index

    def succeed(self, index, value):
        self.jobs.pop(index, None)
        self.pending[index][1](value)

    def fail(self, index, message):
        self.jobs.pop(index, None)
        self.pending[index][2](message)

    def execute(self, index):
        try:
            value = self.pending[index][0]()
        except Exception as exc:
            self.fail(index, str(exc))
        else:
            self.succeed(index, value)


class MockClient:
    def __init__(self, name='a'):
        self.url = 'https://' + name + '.example.test'
        self.calls = []

    def get(self, path):
        self.calls.append(('GET', path))
        return {'scheduler': False} if path == '/health' else []

    def post(self, path, body=None):
        self.calls.append(('POST', path, body))
        return {}

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {}


def record(name):
    return {'id': name, 'module': 'tasks', 'version': 1,
            'data': {'名称': name, '状态': '未开始'}}


def snapshot(name):
    return ({'scheduler': False}, [record(name)],
            {key: [] for key in gui.CONFIG_FIELDS})


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, monkeypatch):
    result = gui.MainWindow(MockClient(), autoload=False)
    result.runner = MockRunner()
    result.error = Mock()
    monkeypatch.setattr(gui.QMessageBox, 'warning', Mock())
    monkeypatch.setattr(gui.QMessageBox, 'information', Mock())
    yield result
    result.runner.jobs.clear()
    result.close()
    result.deleteLater()
    app.processEvents()


def test_refresh_out_of_order_keeps_newest_snapshot(window):
    window.refresh()
    window.refresh()
    window.runner.succeed(1, snapshot('new'))
    window.runner.succeed(0, snapshot('old'))
    assert window.records == [record('new')]


def test_refresh_old_connection_success_is_ignored(window):
    window.refresh()
    window.client = MockClient('b')
    window.refresh()
    window.runner.succeed(1, snapshot('new-server'))
    window.runner.succeed(0, snapshot('old-server'))
    assert window.records == [record('new-server')]
    assert 'b.example.test' in window.connection.text()


def test_refresh_stale_failure_does_not_report_current_connection_failure(window):
    window.refresh()
    window.refresh()
    window.runner.succeed(1, snapshot('new'))
    window.runner.fail(0, '旧请求网络超时')
    window.error.assert_not_called()


def test_refresh_stale_success_does_not_overwrite_newer_failure_status(window):
    # MainWindow.run currently sets success status before loaded rejects stale data.
    window.error = lambda message: window.statusBar().showMessage(message)
    window.refresh()
    window.refresh()
    window.runner.fail(1, '最新刷新失败')
    window.runner.succeed(0, snapshot('old'))
    assert window.statusBar().currentMessage() == '最新刷新失败'


def test_queued_archive_uses_originating_client(window, monkeypatch):
    monkeypatch.setattr(gui, 'ask', lambda *args: True)
    original = window.client
    window.archive(record('old-record'))
    replacement = window.client = MockClient('b')
    window.runner.execute(0)
    assert original.calls == [('DELETE', '/records/old-record?version=1', None)]
    assert replacement.calls == []


def active_widgets(layout):
    """Only installed layout widgets, excluding deferred-delete old pages."""
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item.layout():
            yield from active_widgets(item.layout())
        widget = item.widget()
        if widget:
            yield widget
            yield from widget.findChildren(gui.QWidget)


def test_connection_switch_clears_old_table_before_refresh_finishes(window, monkeypatch):
    window.records = [record('old-record')]
    window.nav.setCurrentRow(window.keys.index('tasks'))
    replacement = MockClient('b')
    monkeypatch.setattr(gui.QInputDialog, 'getText', lambda *a, **k: (replacement.url, True))
    monkeypatch.setattr(gui.QFileDialog, 'getOpenFileName', lambda *a, **k: ('mock-token', ''))
    monkeypatch.setattr(gui.APIClient, 'from_token_file', lambda *a: replacement)
    window.connect_settings()
    # Inspect current layout, not deleteLater widgets retained as QObject children.
    tables = [table for table in active_widgets(window.page_layout)
              if isinstance(table, gui.QTableWidget)]
    assert all(table.rowCount() == 0 for table in tables)
    assert '已连接' not in window.connection.text()


def test_refresh_preserves_active_record_search(window):
    window.records = [record('keep'), record('other')]
    window.nav.setCurrentRow(window.keys.index('tasks'))
    searches = window.page.findChildren(QLineEdit)
    search = next(w for w in searches if w.placeholderText() == '搜索所有字段')
    search.setText('keep')
    window.refresh()
    window.runner.succeed(0, ({'scheduler': False}, window.records, window.config))
    # The newly installed layout must retain the filter, not the deferred old widget.
    fields = list(active_widgets(window.page_layout))
    search = next(w for w in fields if isinstance(w, QLineEdit)
                  and w.placeholderText() == '搜索所有字段')
    assert search.text() == 'keep'


def make_editor(window):
    return gui.FormDialog('测试保存', [('name', '名称', 'text', '')], parent=window)


def start_editor(window, dialog, work):
    # Avoid modal event loop, but use actual Qt controls and connected save signal.
    with patch.object(type(dialog), 'exec', return_value=0):
        window.persist_dialog(dialog, work)
    dialog.widgets['name'][0].setText('用户输入')
    dialog.buttons.accepted.emit()


def test_save_failure_retains_input_and_allows_retry(window):
    dialog = make_editor(window)
    refresh = window.refresh = Mock()
    def save(data):
        raise ClientError('记录已被更新，请刷新后重新编辑。')
    start_editor(window, dialog, save)
    assert not dialog.buttons.isEnabled()
    window.runner.execute(0)
    assert dialog.values()['name'] == '用户输入'
    assert dialog.result() != QDialog.Accepted
    assert dialog.buttons.isEnabled()
    refresh.assert_not_called()
    dialog.buttons.accepted.emit()
    assert len(window.runner.pending) == 2


def test_save_freezes_input_until_request_completes(window):
    dialog = make_editor(window)
    start_editor(window, dialog, lambda data: {})
    # Otherwise edits made after payload capture disappear when done() accepts.
    assert not dialog.widgets['name'][0].isEnabled()
    window.runner.fail(0, '保存失败')
    assert dialog.widgets['name'][0].isEnabled()


def test_save_success_closes_only_after_completion_and_refreshes(window):
    dialog = make_editor(window)
    window.refresh = Mock()
    start_editor(window, dialog, lambda data: {})
    assert dialog.result() != QDialog.Accepted
    window.runner.execute(0)
    assert dialog.result() == QDialog.Accepted
    window.refresh.assert_called_once()


def test_async_callback_failure_reports_error_and_releases_job(app):
    # Call the actual dispatch method, with sender supplied deterministically.
    key = object()
    failed = Mock()
    callback = Mock(side_effect=ValueError('bad UI rendering'))
    owner = SimpleNamespace(jobs={key: (None, callback, failed)}, sender=lambda: key)
    gui.AsyncRunner.finished(owner, {}, None)
    assert owner.jobs == {}
    failed.assert_called_once()
    assert '刷新' in failed.call_args.args[0]


def test_async_callback_failure_with_deleted_error_target_does_not_escape(app):
    key = object()
    callback = Mock(side_effect=RuntimeError('wrapped C/C++ object has been deleted'))
    failed = Mock(side_effect=RuntimeError('error target also deleted'))
    owner = SimpleNamespace(jobs={key: (None, callback, failed)}, sender=lambda: key)
    # Fallback reporting must not propagate a second exception from a Qt slot.
    gui.AsyncRunner.finished(owner, {}, None)
    assert owner.jobs == {}


def test_archive_without_selection_provides_feedback(window):
    before = window.statusBar().currentMessage()
    window.archive(None)
    assert window.error.called or window.statusBar().currentMessage() != before
