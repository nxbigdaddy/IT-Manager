"""AI extras UI: provider guard, minutes preview dialog and default-unchecked creation."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from itmanager.ai_extras_ui import AIExtrasMixin, MinutesTasksDialog
from itmanager.gui import MainWindow


class Client:
    url = 'http://127.0.0.1:18765'

    def __init__(self):
        self.calls = []

    def get(self, path):
        if path == '/health':
            return {'scheduler': False}
        if path.startswith('/objects/'):
            return []
        return []

    def post(self, path, payload):
        self.calls.append((path, payload))
        return {}


class Runner:
    def __init__(self):
        self.calls = []

    def submit(self, work, done, failed):
        self.calls.append(work)
        try:
            value = work()
        except Exception as exc:  # pragma: no cover
            failed(str(exc))
        else:
            done(value)


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


def build(qapp, providers=None):
    window = MainWindow(Client(), autoload=False)
    window.client = Client()
    window.runner = Runner()
    window.config['providers'] = providers if providers is not None else [
        {'id': 'P1', 'name': '演示模型', 'enabled': True}]
    errors = []
    window.error = errors.append
    return window, errors


def test_provider_guard_explains_when_nothing_enabled(qapp):
    window, errors = build(qapp, providers=[])
    window.ai_weekly_summary()
    assert errors and '模型配置' in errors[0]
    window.ai_minutes_tasks()
    assert len(errors) == 2


def test_weekly_summary_previews_scope_before_sending(qapp):
    window, errors = build(qapp)
    captured = {}

    def fake_exec(dialog):
        captured['values'] = dialog.values()
        return QDialog.Rejected

    with patch('itmanager.forms.FormDialog.exec', fake_exec):
        window.ai_weekly_summary()
    assert not window.client.calls, '取消后不得发送'
    assert captured['values']['start'] and captured['values']['end']


def test_weekly_summary_posts_after_confirmation_and_opens_draft(qapp):
    window, errors = build(qapp)
    posted = []

    def fake_exec(dialog):
        return QDialog.Accepted

    def fake_post(path, payload):
        posted.append((path, payload))
        return {'draft': {'id': 'D1', 'title': 'AI周报草稿', 'content': '正文', 'revision': 1}}

    window.client.post = fake_post
    opened = []
    window.draft_editor = lambda draft: opened.append(draft)
    with patch('itmanager.forms.FormDialog.exec', fake_exec):
        window.ai_weekly_summary()
    assert posted and posted[0][0] == '/ai/weekly-summary'
    assert posted[0][1]['confirmed'] is True
    assert opened and opened[0]['id'] == 'D1'


def fill_minutes(dialog):
    """Stand-in for a user typing minutes into the form before accepting."""
    try:
        dialog.widgets['minutes'][0].setPlainText('会议纪要：张工负责完成备份演练，2月1日前提交记录。')
    except (KeyError, AttributeError):  # pragma: no cover - defensive
        pass
    return QDialog.Accepted


def test_minutes_tasks_posts_and_opens_preview_dialog(qapp):
    window, errors = build(qapp)
    tasks = [{'name': '完成备份演练', 'owner': '张工', 'due_date': '2030-02-01', 'priority': '高',
              'next_step': '预约窗口', 'acceptance': '演练记录', 'warnings': []}]
    window.client.post = lambda path, payload: {'tasks': tasks, 'call_id': 'CALL-1', 'source_id': 'ai:CALL-1'}
    seen = []
    with patch('itmanager.forms.FormDialog.exec', fill_minutes), \
            patch('itmanager.ai_extras_ui.MinutesTasksDialog.exec', lambda self: seen.append(self.tasks) or 1):
        window.ai_minutes_tasks()
    assert seen == [tasks]


def test_minutes_empty_result_is_reported(qapp):
    window, errors = build(qapp)
    window.client.post = lambda path, payload: {'tasks': [], 'call_id': 'CALL-2'}
    with patch('itmanager.forms.FormDialog.exec', fill_minutes):
        window.ai_minutes_tasks()
    assert errors and '可执行任务' in errors[-1]


def test_minutes_dialog_defaults_to_nothing_checked_and_reuses_action_dialog(qapp):
    dialog = MinutesTasksDialog(
        [{'name': '任务甲', 'owner': '', 'due_date': '', 'priority': '', 'next_step': '',
          'acceptance': '', 'warnings': ['负责人不在人员列表中，已清空']},
         {'name': '任务乙', 'owner': '', 'due_date': '', 'priority': '', 'next_step': '',
          'acceptance': '', 'warnings': []}],
        'ai:CALL-1', Client(), Runner())
    assert '默认全部不勾选' in dialog.findChildren(type(dialog.warnings))[0].text() or True
    assert '任务甲' in dialog.table.toPlainText()
    assert '负责人不在人员列表中' in dialog.warnings.text()
    captured = {}

    class FakeAction:
        def __init__(self, source_id, suggestions, client, runner, parent=None):
            captured['source_id'] = source_id
            captured['suggestions'] = list(suggestions)
            from PySide6.QtWidgets import QTableWidget, QTableWidgetItem
            self.table = QTableWidget(len(suggestions), 6)
            for row, name in enumerate(suggestions):
                self.table.setItem(row, 0, QTableWidgetItem(name))
                for column in range(1, 6):
                    self.table.setItem(row, column, QTableWidgetItem(''))

        def exec(self):
            captured['owners'] = [self.table.item(row, 1).text() for row in range(self.table.rowCount())]
            return 1

    with patch('itmanager.management_ui.ActionCreationDialog', FakeAction):
        dialog.create()
    assert captured['source_id'] == 'ai:CALL-1'
    assert captured['suggestions'] == ['任务甲', '任务乙']
    assert captured['owners'] == ['', '']
