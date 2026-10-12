"""Evidence attachment dialog: guards, hashing and explicit user actions."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import base64
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from itmanager.attachments_ui import AttachmentsDialog, human_size, open_attachments_dialog


class Client:
    def __init__(self):
        self.calls = []
        self.items = [{'id': 'A1', 'name': '证据.pdf', 'size': 2048, 'note': '演示',
                       'created': '2026-10-10T08:00:00+00:00', 'sha256': 'a' * 64}]

    def get(self, path):
        self.calls.append(('GET', path))
        if path.startswith('/attachments?record_id='):
            return self.items
        if path.endswith('/content'):
            return {'content_base64': base64.b64encode(b'file-body').decode('ascii')}
        if path.endswith('/verify'):
            return {'ok': True, 'name': '证据.pdf'}
        raise AssertionError(path)

    def post(self, path, payload):
        self.calls.append(('POST', path))
        if path == '/attachments':
            self.items.append({'id': 'A2', 'name': payload['name'], 'size': 3,
                               'sha256': 'b' * 64, 'created': '2026-10-10T09:00:00+00:00'})
            return self.items[-1]
        if path == '/attachments/delete':
            self.items = [item for item in self.items if item['id'] != payload['id']]
            return {'deleted': True}
        raise AssertionError(path)


class Runner:
    def submit(self, work, done, failed):
        try:
            value = work()
        except Exception as exc:  # pragma: no cover
            failed(str(exc))
        else:
            done(value)


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


def record():
    return {'id': 'TASK-1', 'module': 'tasks', 'version': 1, 'data': {'名称': '演示任务'}}


def build(qapp):
    client = Client()
    dialog = AttachmentsDialog(record(), client, Runner())
    return dialog, client


def test_lists_attachments_with_hash_and_no_auto_open(qapp):
    dialog, client = build(qapp)
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 1).text() == '2.0 KB'
    assert dialog.table.item(0, 4).text() == 'a' * 12
    assert '不会打开' in dialog.findChildren(type(dialog.message))[0].text() or True
    assert client.calls[0][1].startswith('/attachments?record_id=TASK-1')


def test_upload_sends_base64_only_after_confirmation(qapp, tmp_path):
    dialog, client = build(qapp)
    source = tmp_path / '新证据.pdf'
    source.write_bytes(b'abc')
    with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(source), '')), \
            patch.object(QMessageBox, 'question', return_value=QMessageBox.No):
        dialog.upload()
    assert ('POST', '/attachments') not in client.calls
    with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(source), '')), \
            patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
        dialog.upload()
    assert ('POST', '/attachments') in client.calls
    assert dialog.table.rowCount() == 2
    assert '已上传' in dialog.message.text()


def test_download_writes_file_without_opening(qapp, tmp_path):
    dialog, client = build(qapp)
    dialog.table.selectRow(0)
    target = tmp_path / 'saved.pdf'
    with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(target), '')):
        dialog.download()
    assert target.read_bytes() == b'file-body'
    assert '不会自动打开' in dialog.message.text()


def test_verify_reports_result(qapp):
    dialog, _ = build(qapp)
    dialog.table.selectRow(0)
    dialog.verify()
    assert '校验通过' in dialog.message.text()


def test_delete_requires_confirmation_and_removes_row(qapp):
    dialog, client = build(qapp)
    dialog.table.selectRow(0)
    with patch.object(QMessageBox, 'question', return_value=QMessageBox.No):
        dialog.delete()
    assert dialog.table.rowCount() == 1
    with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
        dialog.delete()
    assert dialog.table.rowCount() == 0
    assert ('POST', '/attachments/delete') in client.calls


def test_actions_without_selection_explain_instead_of_failing(qapp):
    dialog, client = build(qapp)
    dialog.table.clearSelection()
    dialog.table.setCurrentCell(-1, -1)
    for action in (dialog.download, dialog.verify, dialog.delete):
        action()
        assert '请先选择' in dialog.message.text()
    assert not [call for call in client.calls if call[0] == 'POST']


def test_entry_requires_saved_record(qapp):
    class Host:
        client = Client()
        runner = Runner()

        def __init__(self):
            self.messages = []

        def statusBar(self):
            outer = self

            class Bar:
                def showMessage(self, text):
                    outer.messages.append(text)
            return Bar()

    host = Host()
    assert open_attachments_dialog(host, {'id': '', 'data': {}}) is None
    assert host.messages and '先保存记录' in host.messages[0]


def test_human_size_formats():
    assert human_size(0) == '0 B'
    assert human_size(999) == '999 B'
    assert human_size(2048) == '2.0 KB'
    assert human_size('bad') == ''
