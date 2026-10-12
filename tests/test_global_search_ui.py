"""Global-search dialog: scoring, suggestions, module filter and record jump."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication

from itmanager.global_search_ui import GlobalSearchDialog
from itmanager.gui import MainWindow


class Client:
    url = 'http://127.0.0.1:18765'

    def get(self, path):
        return []

    def post(self, path, payload):
        return {}


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


def build(qapp):
    window = MainWindow(Client(), autoload=False)
    window.records = [
        {'id': 'TASK-1', 'module': 'tasks', 'version': 1, 'archived': 0,
         'updated': '2026-10-10T08:00:00Z',
         'data': {'名称': '备份恢复演练', '负责人': '张工', '状态': '进行中',
                  '下一步': '预约演练窗口', '截止日期': '2026-10-20'}},
        {'id': 'SYS-1', 'module': 'systems', 'version': 1, 'archived': 0,
         'updated': '2026-10-09T08:00:00Z',
         'data': {'名称': '财务备份系统', '技术负责人': '李工', '关键性': '高'}},
        {'id': 'PEOPLE-1', 'module': 'people', 'version': 1, 'archived': 0,
         'updated': '2026-10-08T08:00:00Z', 'data': {'姓名': '张工', '部门': 'IT'}},
    ]
    return window


def test_search_populates_ranked_results_and_summary(qapp):
    window = build(qapp)
    dialog = GlobalSearchDialog(window, '备份 张工')
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 1).text() == '备份恢复演练'
    assert '任务管理 1' in dialog.summary.text()
    assert '【' in dialog.table.item(0, 6).text()


def test_module_filter_and_empty_query(qapp):
    window = build(qapp)
    dialog = GlobalSearchDialog(window, '')
    dialog.run_search()
    assert dialog.table.rowCount() == 0
    assert '空搜索不会展示全库内容' in dialog.summary.text()
    dialog.query.setText('备份')
    dialog.module.setCurrentIndex(dialog.module.findData('systems'))
    dialog.run_search()
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 0).text() == '系统台账'


def test_double_click_jumps_and_opens_record(qapp):
    window = build(qapp)
    dialog = GlobalSearchDialog(window, '备份恢复演练')
    dialog.table.selectRow(0)
    opened = []
    window.edit_record = lambda module, record: opened.append((module, record['id']))
    dialog.open_selected()
    assert window.current == 'tasks'
    assert opened == [('tasks', 'TASK-1')]


def test_toolbar_and_shortcut_open_search(qapp, monkeypatch):
    window = build(qapp)
    assert window.global_search.objectName() == 'global_search'
    window.global_search.setText('张工')
    captured = []
    monkeypatch.setattr('itmanager.global_search_ui.open_global_search',
                        lambda host, query='': captured.append(query) or None)
    window.open_global_search()
    assert captured == ['张工']
    window.show(); qapp.processEvents()
    window.focus_global_search(); qapp.processEvents()
    assert window.global_search.hasFocus()
    assert any(shortcut.key().toString() == 'Ctrl+Shift+F' for shortcut in window.shortcuts)
