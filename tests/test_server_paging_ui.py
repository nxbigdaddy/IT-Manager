"""Large ledgers switch the record list to server-side paging."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from unittest.mock import patch

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QTableWidget

from itmanager.gui import MainWindow
from itmanager.workspace_ui import SERVER_PAGING_THRESHOLD


class Client:
    url = 'http://127.0.0.1:18765'

    def __init__(self, total=1):
        self.calls = []
        self.total = total

    def get(self, path):
        self.calls.append(path)
        if path.startswith('/records/page?'):
            offset = 0
            for part in path.split('?', 1)[1].split('&'):
                if part.startswith('offset='):
                    offset = int(part.split('=', 1)[1])
            rows = [{'id': f'PAGE-{offset + index}', 'module': 'tasks', 'version': 1, 'archived': 0,
                     'data': {'名称': f'分页任务{offset + index}', '状态': '进行中'}}
                    for index in range(min(100, max(0, self.total - offset)))]
            return {'records': rows, 'total': self.total, 'limit': 100, 'offset': offset,
                    'has_more': offset + 100 < self.total}
        return []

    def post(self, path, payload):
        self.calls.append(path)
        return {}


class Runner:
    """Synchronous runner: work() runs immediately so assertions see final state."""

    def submit(self, work, done, failed):
        try:
            value = work()
        except Exception as exc:  # pragma: no cover
            failed(str(exc))
        else:
            if done:
                done(value)


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


def build(qapp, count):
    window = MainWindow(Client(total=count), autoload=False)
    window.runner = Runner()
    window.records = [{'id': f'BULK-{index}', 'module': 'tasks', 'version': 1, 'archived': 0,
                       'data': {'名称': f'批量任务{index}', '状态': '进行中'}} for index in range(count)]
    window.navigate(window.keys.index('tasks'))
    return window


def labels(window):
    return [label.text() for label in window.page.findChildren(QLabel)]


def test_large_dataset_uses_server_paging(qapp):
    window = build(qapp, SERVER_PAGING_THRESHOLD + 1)
    assert any('服务端检索' in text for text in labels(window))
    assert any(path.startswith('/records/page?') for path in window.client.calls)
    tables = [table for table in window.page.findChildren(QTableWidget) if table.rowCount()]
    assert tables, 'server page rows must be rendered'
    assert tables[0].rowCount() == 100
    assert any('共 1001 条' in text for text in labels(window))
    assert not any('BULK-' in str(table.item(0, 0).data(0x0100)) for table in tables)


def test_small_dataset_keeps_local_filtering(qapp):
    window = build(qapp, 5)
    assert not any('服务端检索' in text for text in labels(window)) or len(window.records) <= SERVER_PAGING_THRESHOLD
    assert not any(path.startswith('/records/page?') for path in window.client.calls)
    tables = [table for table in window.page.findChildren(QTableWidget) if table.rowCount()]
    assert tables and tables[0].rowCount() == 5


def test_paging_navigation_requests_offsets(qapp):
    total = SERVER_PAGING_THRESHOLD + 5
    window = build(qapp, total)
    assert any('offset=0' in path for path in window.client.calls)
    from PySide6.QtWidgets import QPushButton
    buttons = {button.text(): button for button in window.page.findChildren(QPushButton)}
    buttons['下一页'].click()
    assert any('offset=100' in path for path in window.client.calls)
    buttons['上一页'].click()
    assert any('offset=0' in path for path in window.client.calls)
    assert any(f'共 {total} 条' in text for text in labels(window))
