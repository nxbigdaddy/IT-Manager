"""Lifecycle dialog: stages, gaps and the explicit "coverage is not resolution" wording."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication

from itmanager.lifecycle_ui import LifecycleDialog, open_lifecycle_dialog


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


def ledger():
    return [
        {'id': 'INC-1', 'module': 'incidents', 'version': 1, 'archived': 0,
         'updated': '2026-10-01T00:00:00Z',
         'data': {'名称': '备份作业失败', '状态': '进行中', '关联ID': 'RISK-1', '截止日期': '2026-10-05'}},
        {'id': 'RISK-1', 'module': 'risks', 'version': 1, 'archived': 0,
         'updated': '2026-10-01T00:00:00Z',
         'data': {'名称': '备份链路风险', '等级': '高', '状态': '进行中', '关联ID': 'CHG-1'}},
        {'id': 'CHG-1', 'module': 'changes', 'version': 1, 'archived': 0,
         'updated': '2026-10-01T00:00:00Z',
         'data': {'名称': '更换备份介质', '状态': '已完成', '截止日期': '2026-10-03'}},
        {'id': 'TASK-1', 'module': 'tasks', 'version': 1, 'archived': 0,
         'updated': '2026-10-01T00:00:00Z',
         'data': {'名称': '恢复演练验证', '状态': '已完成', '关联ID': 'CHG-1'}},
    ]


def test_dialog_lists_stages_and_gaps(qapp):
    dialog = LifecycleDialog(ledger(), 'INC-1')
    text = dialog.describe()
    assert '事件' in text and '根因' in text and '变更' in text and '验证' in text
    assert '备份作业失败' in text and '更换备份介质' in text
    assert '缺口提示' in text
    assert '恢复演练验证' in text
    from PySide6.QtWidgets import QLabel
    labels = ' '.join(label.text() for label in dialog.findChildren(QLabel))
    assert '不代表问题已经解决' in labels


def test_dialog_reports_missing_record_instead_of_crashing(qapp):
    dialog = LifecycleDialog(ledger(), 'MISSING-1')
    assert '无法构建链条' in dialog.describe()


def test_entry_requires_saved_record(qapp):
    class Host:
        records = ledger()

        def __init__(self):
            self.messages = []

        def statusBar(self):
            outer = self

            class Bar:
                def showMessage(self, text):
                    outer.messages.append(text)
            return Bar()

    host = Host()
    assert open_lifecycle_dialog(host, {'id': ''}) is None
    assert host.messages and '已保存的记录' in host.messages[0]
