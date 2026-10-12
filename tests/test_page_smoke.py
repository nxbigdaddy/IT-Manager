"""Smoke test: every navigation entry renders without raising, in both themes."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication

from itmanager.gui import MainWindow


class Client:
    url = 'http://127.0.0.1:18765'

    def get(self, path):
        if path == '/health':
            return {'scheduler': True, 'scheduler_status': {'last_tick': '2026-10-10T00:00:00Z', 'error': None}}
        if path == '/records/page?' or path.startswith('/records/page?'):
            return {'records': [], 'total': 0, 'limit': 100, 'offset': 0, 'has_more': False}
        if path == '/attachments?record_id=DEMO':
            return []
        if path == '/backup/status':
            return {'health': {'ok': True, 'reasons': [], 'newest': 'demo.sqlite3', 'age_days': 1, 'missed': 0},
                    'retention': {'keep': [], 'delete': []}, 'files': [], 'schedule': None, 'runs': []}
        if path == '/reminders':
            return []
        if path == '/reminders/states' or path == '/health/states':
            return []
        return []

    def post(self, path, payload):
        return {}


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


def build(qapp):
    window = MainWindow(Client(), autoload=False)
    window.error = lambda message: errors.append(str(message))
    window.records = [
        {'id': 'DEMO-TASK', 'module': 'tasks', 'version': 1, 'archived': 0,
         'updated': '2026-10-09T00:00:00Z',
         'data': {'名称': '演示任务', '状态': '进行中', '负责人': '甲', '截止日期': '2030-01-01'}},
        {'id': 'DEMO-PERSON', 'module': 'people', 'version': 1, 'archived': 0,
         'updated': '2026-10-09T00:00:00Z', 'data': {'姓名': '甲'}},
        {'id': 'DEMO-SYSTEM', 'module': 'systems', 'version': 1, 'archived': 0,
         'updated': '2026-10-09T00:00:00Z', 'data': {'名称': '演示系统', '关键性': '高'}},
    ]
    window.config['providers'] = [{'id': 'P1', 'name': '演示模型', 'enabled': True}]
    window.config['channels'] = [{'id': 'C1', 'name': '演示渠道', 'type': 'wecom', 'enabled': True}]
    window.config['prompts'] = [{'id': 'PR1', 'name': '演示提示词', 'enabled': True, 'purpose': 'draft'}]
    window.config['schedules'] = [{'id': 'S1', 'name': '演示计划', 'kind': 'report', 'enabled': False,
                                   'next_run': '2030-01-01T00:00:00+00:00'}]
    errors = []
    window.error = errors.append
    return window, errors


def test_every_navigation_entry_renders(qapp):
    window, errors = build(qapp)
    for index, key in enumerate(window.keys):
        window.nav.setCurrentRow(index)
        qapp.processEvents()
        assert window.current == key
        assert window.page_layout.count() >= 1, key
    assert errors == [], errors


def test_every_page_renders_in_dark_theme_and_large_font(qapp):
    window, errors = build(qapp)
    window.prefs.theme = 'dark'
    window.prefs.font_scale = 1.3
    window.apply_theme()
    for index in range(len(window.keys)):
        window.nav.setCurrentRow(index)
        qapp.processEvents()
    assert '#f5f7fb' not in window.styleSheet()
    assert 'font-size:17px' in window.styleSheet()
    window.prefs.theme = 'light'
    window.prefs.font_scale = 1.0
    window.apply_theme()
    assert errors == [], errors


def test_record_and_draft_pages_expose_the_expected_actions(qapp):
    window, errors = build(qapp)
    from PySide6.QtWidgets import QPushButton
    window.nav.setCurrentRow(window.keys.index('drafts'))
    qapp.processEvents()
    labels = {button.text() for button in window.page.findChildren(QPushButton)}
    assert any('AI周报汇总' in text for text in labels)
    assert any('会议记录' in text or '会议纪要转任务' in text for text in labels)
    window.nav.setCurrentRow(window.keys.index('tasks'))
    qapp.processEvents()
    labels = {button.text() for button in window.page.findChildren(QPushButton)}
    for expected in ('批量操作…', '证据附件…', '关联链条…', '推进任务'):
        assert any(expected in text for text in labels), expected
    assert errors == []
