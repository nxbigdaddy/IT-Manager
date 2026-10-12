"""Batch operations and filtered export wired to an existing record table."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QSpinBox, QVBoxLayout,
)
from PySide6.QtCore import QDate

from . import batch_ops
from .schema import MODULES, STATUS


def selected_records(table):
    """Records behind the selected table rows, de-duplicated and order-stable."""
    seen, rows = set(), []
    for index in sorted(table.selectionModel().selectedRows(), key=lambda i: i.row()):
        item = table.item(index.row(), 0)
        record = item.data(Qt.UserRole) if item else None
        if isinstance(record, dict) and record.get('id') not in seen:
            seen.add(record['id'])
            rows.append(record)
    if rows:
        return rows
    return []  # Batch operations require an explicit multi-row selection.


def write_export(path, module, rows):
    """Write the current filtered selection as CSV or a single-sheet workbook."""
    payload = batch_ops.export_rows(rows, module)
    target = Path(path)
    if target.suffix.lower() == '.xlsx':
        from openpyxl import Workbook
        book = Workbook()
        sheet = book.active
        sheet.title = MODULES[module][0][:28]
        sheet.append(payload['headers'])
        for row in payload['rows']:
            sheet.append(row)
            for cell in sheet[sheet.max_row]:
                cell.data_type = 's'
        sheet.freeze_panes = 'B2'
        sheet.auto_filter.ref = sheet.dimensions
        book.save(target)
        return target
    target.write_text(batch_ops.to_csv(payload['headers'], payload['rows']), encoding='utf-8-sig')
    return target


class BatchDialog(QDialog):
    """Preview-then-apply batch editing; never claims partial success as success."""

    MODES = ('批量修改字段', '批量顺延日期', '批量归档', '导出当前选择')

    def __init__(self, module, records, client, runner, parent=None):
        super().__init__(parent)
        self.module, self.records = module, list(records)
        self.client, self.runner = client, runner
        self.busy = False
        self.request = {'fingerprint': None, 'id': None}
        self.setWindowTitle('批量操作 · ' + MODULES[module][0])
        self.resize(760, 620)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f'已选择 {len(self.records)} 条记录。批量操作按版本校验，任一记录冲突则整批不写入。'))
        form = QFormLayout()
        layout.addLayout(form)
        self.mode = QComboBox()
        self.mode.addItems(self.MODES)
        form.addRow('操作', self.mode)
        self.field = QComboBox()
        self.field.addItems(batch_ops.batchable_fields(module))
        form.addRow('字段', self.field)
        self.value = QComboBox()
        self.value.setEditable(True)
        form.addRow('值', self.value)
        self.days = QSpinBox()
        self.days.setRange(1, 3650)
        self.days.setValue(30)
        form.addRow('顺延天数', self.days)
        self.fixed = QCheckBox('使用固定日期')
        self.fixed_date = QDateEdit(QDate.currentDate().addDays(30))
        self.fixed_date.setCalendarPopup(True)
        self.fixed_date.setDisplayFormat('yyyy-MM-dd')
        row = QHBoxLayout()
        row.addWidget(self.fixed)
        row.addWidget(self.fixed_date)
        form.addRow('固定日期', row)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText('点击“预览计划”查看将要修改的内容与跳过原因。')
        layout.addWidget(self.preview, 1)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QDialogButtonBox()
        self.preview_button = buttons.addButton('预览计划', QDialogButtonBox.ActionRole)
        self.apply_button = buttons.addButton('执行', QDialogButtonBox.AcceptRole)
        buttons.addButton('关闭', QDialogButtonBox.RejectRole)
        layout.addWidget(buttons)
        self.preview_button.clicked.connect(self.show_preview)
        self.apply_button.clicked.connect(self.apply)
        buttons.rejected.connect(self.reject)
        self.mode.currentIndexChanged.connect(self.sync_mode)
        self.field.currentIndexChanged.connect(self.sync_mode)
        self.sync_mode()

    # ---------------------------------------------------------------- helpers
    def sync_mode(self):
        mode = self.mode.currentText()
        field_mode = mode == self.MODES[0]
        postpone = mode == self.MODES[1]
        export = mode == self.MODES[3]
        self.field.setEnabled(field_mode)
        self.value.setEnabled(field_mode)
        self.days.setEnabled(postpone)
        self.fixed.setEnabled(postpone)
        self.fixed_date.setEnabled(postpone and self.fixed.isChecked())
        self.apply_button.setText('导出…' if export else '执行')
        if field_mode:
            choices = STATUS if self.field.currentText() == '状态' else []
            self.value.clear()
            self.value.addItems([str(choice) for choice in choices])
            self.value.setEditable(not choices)
        self.preview.setPlainText('')

    def plan(self):
        mode = self.mode.currentText()
        if mode == self.MODES[0]:
            return batch_ops.plan_field_update(self.records, self.module, self.field.currentText(),
                                               self.value.currentText())
        if mode == self.MODES[1]:
            fixed = self.fixed_date.date().toString('yyyy-MM-dd') if self.fixed.isChecked() else None
            return batch_ops.plan_postpone(self.records, self.module, self.days.value(),
                                           new_date=fixed, today=date.today())
        if mode == self.MODES[2]:
            return batch_ops.plan_archive(self.records)
        return batch_ops.export_rows(self.records, self.module)

    def describe(self, plan):
        mode = self.mode.currentText()
        if mode == self.MODES[3]:
            return f'导出 {len(plan["rows"])} 行，列：{", ".join(plan["headers"])}'
        lines = []
        for key in ('changes', 'targets'):
            if plan.get(key):
                lines.append(f'将处理 {len(plan[key])} 条：')
                lines.extend('  · ' + json.dumps({k: v for k, v in item.items() if k != "data"},
                                                 ensure_ascii=False) for item in plan[key][:20])
        if plan.get('skipped'):
            lines.append(f'跳过 {len(plan["skipped"])} 条：')
            lines.extend('  · ' + json.dumps(item, ensure_ascii=False) for item in plan['skipped'][:20])
        if not lines:
            lines.append('没有可执行的变更。')
        return '\n'.join(lines)

    def show_preview(self):
        try:
            plan = self.plan()
        except ValueError as exc:
            self.message.setText(str(exc))
            return
        self.message.setText('')
        self.preview.setPlainText(self.describe(plan))

    # ------------------------------------------------------------------- apply
    def apply(self):
        if self.busy:
            return
        try:
            plan = self.plan()
        except ValueError as exc:
            self.message.setText(str(exc))
            return
        mode = self.mode.currentText()
        if mode == self.MODES[3]:
            self.export(plan)
            return
        if mode == self.MODES[2]:
            payload = {'targets': plan['targets'], 'request_id': None}
            path = '/records/batch-archive'
            count = len(payload['targets'])
        else:
            payload = {'updates': plan['changes'], 'request_id': None}
            path = '/records/batch'
            count = len(payload['updates'])
        if not count:
            self.message.setText('没有可执行的变更；请先查看预览中的跳过原因。')
            return
        if mode == self.MODES[2]:
            text = f'将归档 {count} 条记录（可恢复历史，不会删除数据）。继续？'
        else:
            text = f'将修改 {count} 条记录，任一记录版本冲突则整批不写入。继续？'
        if QMessageBox.question(self, '确认批量操作', text,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        key = 'targets' if mode == self.MODES[2] else 'updates'
        fingerprint = json.dumps(payload[key], ensure_ascii=False, sort_keys=True)
        if fingerprint != self.request['fingerprint']:
            self.request = {'fingerprint': fingerprint, 'id': uuid4().hex}
        payload['request_id'] = self.request['id']
        self.busy = True
        self.apply_button.setEnabled(False)
        self.preview_button.setEnabled(False)

        def done(value):
            self.busy = False
            self.apply_button.setEnabled(True)
            self.preview_button.setEnabled(True)
            self.message.setText(f'完成：{value.get("count", 0)} 条已写入。')
            self.request = {'fingerprint': None, 'id': None}
            self.accept()

        def failed(message):
            self.busy = False
            self.apply_button.setEnabled(True)
            self.preview_button.setEnabled(True)
            self.message.setText('批量操作失败：' + str(message) + '（本次请求编号已保留，重试不会重复写入）')

        client = self.client
        self.runner.submit(lambda: client.post(path, payload), done, failed)

    def export(self, plan):
        suggested = batch_ops.safe_filename(self.module, date.today().isoformat())
        path, _ = QFileDialog.getSaveFileName(self, '导出当前选择', suggested,
                                              'Excel (*.xlsx);;CSV (*.csv)')
        if not path:
            return
        try:
            target = write_export(path, self.module, self.records)
        except (OSError, ValueError, ImportError) as exc:
            self.message.setText('导出失败：' + str(exc))
            return
        self.message.setText(f'已导出 {len(plan["rows"])} 行到 {target.name}。')
        self.accept()


class BatchMixin:
    """Adds the row-selection batch entry point to the record list page."""

    def batch_actions(self, module, table):
        records = selected_records(table)
        if not records:
            self.statusBar().showMessage('请先按住 Ctrl / Shift 选择要批量处理的记录。')
            return
        dialog = BatchDialog(module, records, self.client, self.runner, self)
        dialog.exec()
        if not dialog.busy:
            self.refresh()
