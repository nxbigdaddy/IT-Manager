"""Evidence attachments dialog: upload, verify, download, delete — never auto-opens files."""
from __future__ import annotations

import base64
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

COLUMNS = ['名称', '大小', '说明', '上传时间', 'SHA-256（前12位）']


def human_size(size):
    try:
        value = float(size)
    except (TypeError, ValueError):
        return ''
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024
    return ''


class AttachmentsDialog(QDialog):
    """Attachments for one record; every action is explicit and reversible except delete."""

    def __init__(self, record, client, runner, parent=None):
        super().__init__(parent)
        self.record = record
        self.client = client
        self.runner = runner
        self.busy = False
        self.items = []
        self.setWindowTitle('证据附件 · ' + str(record.get('data', {}).get('名称', record.get('id', ''))))
        self.resize(880, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('附件保存在服务端数据目录，记录文件大小与 SHA-256。'
                                '系统不会打开、执行或预览任何附件；下载后请自行用受控软件查看，'
                                '附件不是合规归档系统，仍需按留存要求自行备份。'))
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.message = QLabel('')
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        tools = QHBoxLayout()
        layout.addLayout(tools)
        self.upload_button = QPushButton('上传附件…')
        self.download_button = QPushButton('下载所选…')
        self.verify_button = QPushButton('完整性校验')
        self.delete_button = QPushButton('删除所选')
        for button in (self.upload_button, self.download_button, self.verify_button, self.delete_button):
            tools.addWidget(button)
        tools.addStretch()
        close = QPushButton('关闭')
        tools.addWidget(close)
        self.upload_button.clicked.connect(self.upload)
        self.download_button.clicked.connect(self.download)
        self.verify_button.clicked.connect(self.verify)
        self.delete_button.clicked.connect(self.delete)
        close.clicked.connect(self.reject)
        self.refresh()

    # ------------------------------------------------------------------ helpers
    def selected(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.items):
            return None
        return self.items[row]

    def set_busy(self, busy):
        self.busy = busy
        for button in (self.upload_button, self.download_button, self.verify_button, self.delete_button):
            button.setEnabled(not busy)
        self.table.setEnabled(not busy)

    def call(self, work, done, failure_text='操作失败'):
        if self.busy:
            return
        self.set_busy(True)

        def finish(message=None):
            self.set_busy(False)
            if message:
                self.message.setText(message)

        def ok(value):
            finish()
            done(value)

        def bad(message):
            finish(f'{failure_text}：{message}')

        self.runner.submit(work, ok, bad)

    def refresh(self):
        def done(value):
            self.items = value if isinstance(value, list) else []
            self.table.setRowCount(len(self.items))
            for index, item in enumerate(self.items):
                values = [item.get('name', ''), human_size(item.get('size')), item.get('note', ''),
                          str(item.get('created', ''))[:19], str(item.get('sha256', ''))[:12]]
                for column, text in enumerate(values):
                    cell = QTableWidgetItem(str(text))
                    cell.setData(Qt.UserRole, item)
                    self.table.setItem(index, column, cell)
            if not self.items:
                self.message.setText('该记录还没有附件。证据字段仍可填写文本索引；'
                                     '需要保管真实文件时再上传，并自行确认文件的敏感级别。')

        client = self.client
        self.call(lambda: client.get('/attachments?record_id=' + str(self.record['id'])), done,
                  failure_text='读取附件列表失败')

    # ------------------------------------------------------------------ actions
    def upload(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择证据文件', '', '所有文件 (*)')
        if not path:
            return
        source = Path(path)
        try:
            content = source.read_bytes()
        except OSError as exc:
            self.message.setText('读取文件失败：' + str(exc))
            return
        if QMessageBox.question(self, '确认上传',
                                f'上传“{source.name}”（{human_size(len(content))}）到服务端？\n'
                                '上传后系统只保存文件与哈希，不会打开或执行它。',
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        payload = {'record_id': self.record['id'], 'name': source.name,
                   'content_base64': base64.b64encode(content).decode('ascii'),
                   'note': '上传自客户端'}
        client = self.client

        def done(value):
            self.message.setText(f"已上传：{value.get('name', '')}（SHA-256 {str(value.get('sha256', ''))[:12]}…）")
            self.refresh()

        self.call(lambda: client.post('/attachments', payload), done, failure_text='上传失败')

    def download(self):
        item = self.selected()
        if not item:
            self.message.setText('请先选择一个附件。')
            return
        target, _ = QFileDialog.getSaveFileName(self, '保存附件', item.get('name', ''), '所有文件 (*)')
        if not target:
            return
        client = self.client

        def done(value):
            try:
                content = base64.b64decode(value.get('content_base64', ''))
                Path(target).write_bytes(content)
            except (OSError, ValueError) as exc:
                self.message.setText('写入文件失败：' + str(exc))
                return
            self.message.setText(f'已保存到 {target}。系统不会自动打开该文件，请用受控软件查看。')

        self.call(lambda: client.get(f"/attachments/{item['id']}/content"), done, failure_text='下载失败')

    def verify(self):
        item = self.selected()
        if not item:
            self.message.setText('请先选择一个附件。')
            return
        client = self.client

        def done(value):
            ok = bool(value.get('ok'))
            self.message.setText(('完整性校验通过：' if ok else '完整性校验失败（文件可能被改动或丢失）：')
                                 + str(value.get('name', '')))

        self.call(lambda: client.get(f"/attachments/{item['id']}/verify"), done, failure_text='校验失败')

    def delete(self):
        item = self.selected()
        if not item:
            self.message.setText('请先选择一个附件。')
            return
        if QMessageBox.question(self, '删除附件',
                                f"删除附件“{item.get('name', '')}”？\n"
                                '文件会从服务端数据目录移除；业务记录其它字段不受影响，'
                                '但删除后无法从本系统恢复。',
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        client = self.client

        def done(value):
            self.message.setText('附件已删除。')
            self.refresh()

        self.call(lambda: client.post('/attachments/delete', {'id': item['id']}), done, failure_text='删除失败')


def open_attachments_dialog(host, record, client=None, runner=None):
    """入口：只为已有记录提供附件管理（新建记录尚未入库时没有记录ID）。"""
    if not record or not record.get('id'):
        host.statusBar().showMessage('请先保存记录，再上传证据附件。')
        return None
    dialog = AttachmentsDialog(record, client or host.client, runner or host.runner, host)
    dialog.exec()
    return dialog
