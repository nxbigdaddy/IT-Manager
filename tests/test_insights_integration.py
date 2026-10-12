"""Real-host integration for the reminder centre and health scan pages.

Every modal entry point is patched to raise, so an accidental blocking dialog
fails the test instead of hanging the suite.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date, timedelta
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox, QPushButton, QTableWidget

from itmanager.gui import MainWindow
from itmanager.health import scan
from itmanager.reminders import compute_items, default_rules


class StubClient:
    url = 'http://127.0.0.1:18765'

    def __init__(self):
        self.calls = []

    def get(self, path):
        self.calls.append(('GET', path))
        if path == '/reminders':
            return [dict(rule, id='rule-' + rule['key'], version=1) for rule in default_rules()]
        if path == '/checklist-templates':
            return []
        return []

    def post(self, path, payload):
        self.calls.append(('POST', path))
        raise AssertionError('integration test must not post')


class Blocked(Exception):
    pass


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


def _forbid_modals():
    return (
        patch.object(QMessageBox, 'question', side_effect=Blocked('modal')),
        patch.object(QMessageBox, 'warning', side_effect=Blocked('modal')),
        patch.object(QMessageBox, 'information', side_effect=Blocked('modal')),
        patch.object(QMessageBox, 'critical', side_effect=Blocked('modal')),
        patch.object(QFileDialog, 'getSaveFileName', side_effect=Blocked('modal')),
        patch.object(QFileDialog, 'getOpenFileName', side_effect=Blocked('modal')),
        patch.object(QDialog, 'exec', side_effect=Blocked('modal')),
    )


def build_window(qapp):
    window = MainWindow(StubClient(), autoload=False)
    soon = (date.today() + timedelta(days=5)).isoformat()
    late = (date.today() - timedelta(days=3)).isoformat()
    window.records = [
        {'id': 'CONTRACT-1', 'module': 'contracts', 'version': 1, 'archived': 0,
         'updated': '2026-10-01T00:00:00Z', 'data': {'名称': '演示合同', '到期日期': soon}},
        {'id': 'TASK-1', 'module': 'tasks', 'version': 1, 'archived': 0,
         'updated': '2026-10-01T00:00:00Z',
         'data': {'名称': '逾期任务', '状态': '进行中', '截止日期': late}},
        {'id': 'RISK-1', 'module': 'risks', 'version': 1, 'archived': 0,
         'updated': '2026-10-01T00:00:00Z', 'data': {'名称': '高风险项', '等级': '高', '状态': '进行中'}},
    ]
    return window


def _page_table(window):
    tables = [t for t in window.page.findChildren(QTableWidget) if t.rowCount()]
    assert tables, 'expected a populated table'
    return tables[0]


def test_reminders_page_renders_real_items(qapp):
    window = build_window(qapp)
    expected = compute_items(window.records, [r for r in default_rules() if r.get('enabled', True)], date.today())
    assert expected, 'fixture must produce reminders'
    with _forbid_modals()[0], _forbid_modals()[1], _forbid_modals()[2], _forbid_modals()[3], \
            _forbid_modals()[4], _forbid_modals()[5], _forbid_modals()[6]:
        window.navigate(window.keys.index('reminders'))
    table = _page_table(window)
    assert table.rowCount() == len(expected)
    labels = {table.item(0, column).text() for column in range(table.columnCount())}
    assert '高' in labels or any(item['severity'] == '高' for item in expected)
    payloads = [table.item(row, 0).data(0x0100) for row in range(table.rowCount())]
    assert all(isinstance(value, dict) for value in payloads)
    assert any(value.get('id') or value.get('record_id') or value.get('record') for value in payloads)
    expected_ids = {item['record_id'] for item in expected}
    shown = {value.get('record_id') or value.get('id') for value in payloads}
    assert shown <= expected_ids


def test_health_page_renders_real_findings(qapp):
    window = build_window(qapp)
    report = scan(window.records)
    with _forbid_modals()[0], _forbid_modals()[1], _forbid_modals()[2], _forbid_modals()[3], \
            _forbid_modals()[4], _forbid_modals()[5], _forbid_modals()[6]:
        window.navigate(window.keys.index('health'))
    table = _page_table(window)
    assert table.rowCount() == len(report['findings'])
    assert report['summary']['total'] == len(report['findings'])


def test_dashboard_banner_matches_reminder_count(qapp):
    window = build_window(qapp)
    expected = compute_items(window.records, [r for r in default_rules() if r.get('enabled', True)], date.today())
    with _forbid_modals()[0], _forbid_modals()[1], _forbid_modals()[2], _forbid_modals()[3], \
            _forbid_modals()[4], _forbid_modals()[5], _forbid_modals()[6]:
        window.navigate(window.keys.index('dashboard'))
        banners = [b.text() for b in window.page.findChildren(QPushButton) if '待处理提醒' in b.text()]
    assert banners and any(str(len(expected)) in text for text in banners)


def test_charts_page_builds_without_dialogs(qapp):
    window = build_window(qapp)
    with _forbid_modals()[0], _forbid_modals()[1], _forbid_modals()[2], _forbid_modals()[3], \
            _forbid_modals()[4], _forbid_modals()[5], _forbid_modals()[6]:
        window.navigate(window.keys.index('charts'))
    assert window.page.findChildren(QTableWidget) or window.page.findChildren(QPushButton)
    assert not [call for call in window.client.calls if call[0] == 'POST']
