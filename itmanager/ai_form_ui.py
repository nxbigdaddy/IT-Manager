"""Opt-in AI form assistance; never saves records or creates related objects."""
from __future__ import annotations

import json
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)
from .store import REFERENCES

SENSITIVE = {'people', 'reviews', 'access'}


def _empty(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _widget(entry):
    return entry[0] if isinstance(entry, (tuple, list)) else entry


def _value(widget):
    if isinstance(widget, QComboBox):
        if widget.isEditable():
            return '' if widget.currentData() == '' and widget.currentText() == '（无）' else widget.currentText()
        return widget.currentData()
    if isinstance(widget, QPlainTextEdit):
        return widget.toPlainText()
    if isinstance(widget, QLineEdit):
        return widget.text()
    return None


def _finalized(dialog):
    return bool(getattr(dialog, '_final_review', False)) or getattr(dialog, 'record', {}).get('data', {}).get('状态') == '已定稿'


def apply_fields(dialog, fields, selected_keys, overwrite=False):
    """Return applied key list and skipped key→reason; leave dirty baselines alone."""
    applied, skipped = [], {}
    for key in dict.fromkeys(selected_keys):
        entry = getattr(dialog, 'widgets', {}).get(key)
        if _finalized(dialog):
            skipped[key] = '已定稿，禁止AI填写'
            continue
        if entry is None or key not in fields:
            skipped[key] = '不存在的字段'
            continue
        widget = _widget(entry)
        if (not widget.isEnabled() or key in getattr(dialog, '_locked_keys', set())
                or key in getattr(dialog, '_hidden_keys', set())
                or (hasattr(widget, 'isReadOnly') and widget.isReadOnly())
                or (isinstance(widget, QComboBox) and widget.isEditable() and widget.lineEdit().isReadOnly())):
            skipped[key] = '字段只读或禁用'
            continue
        value = fields[key]
        if value is not None and not isinstance(value, (str, int, float, bool)):
            skipped[key] = '建议不是有效字段值'
            continue
        if not overwrite and not _empty(_value(widget)):
            skipped[key] = '已有值，未经覆盖授权'
            continue
        text = '' if value is None else str(value)
        if isinstance(widget, QComboBox):
            index = widget.findData(value)
            if index < 0 and key not in REFERENCES:
                index = widget.findText(text)
            if index >= 0:
                widget.setCurrentIndex(index)
            elif widget.isEditable() and key not in REFERENCES:
                widget.setEditText(text)
            else:
                skipped[key] = '未知关联或选项；请手工核实，不会创建新选项'
                continue
        elif isinstance(widget, QPlainTextEdit):
            widget.setPlainText(text)
        elif isinstance(widget, QLineEdit):
            widget.setText(text)
        else:
            skipped[key] = '不支持的字段类型'
            continue
        applied.append(key)
    return {'applied': applied, 'skipped': skipped}


def ask(parent, text):
    return QMessageBox.question(parent, '外发授权 / 确认', text,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes


class AIFormDialog(QDialog):
    def __init__(self, form, module, records, providers, prompts, runner, client):
        super().__init__(form)
        self.form, self.module = form, module
        self.runner, self.client = runner, client
        self.busy = False
        self.fields = {}
        self._released = False
        self._parent_busy = getattr(form, 'busy', False)
        self.setWindowTitle('AI辅助填写（仅填入，不保存）')
        self.setWindowModality(Qt.WindowModal)
        self.resize(820, 760)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('基础事项（多行描述）：'))
        self.basis = QPlainTextEdit()
        self.basis.setObjectName('ai_basis')
        layout.addWidget(self.basis)
        self.provider = QComboBox()
        for row in providers:
            if row.get('enabled', True):
                self.provider.addItem(str(row.get('name') or row.get('model') or row.get('id')), row)
        layout.addWidget(QLabel('外部模型：'))
        layout.addWidget(self.provider)
        self.prompt = QComboBox()
        for row in prompts:
            if row.get('enabled', True) and row.get('purpose') == 'form_fill' and row.get('module') == module:
                self.prompt.addItem(str(row.get('name') or row.get('id')), row)
                if row.get('is_default'):
                    self.prompt.setCurrentIndex(self.prompt.count() - 1)
        layout.addWidget(QLabel('填写提示词：'))
        layout.addWidget(self.prompt)
        layout.addWidget(QLabel('可选上下文（默认不选，最多30条；仅勾选记录会外发）：'))
        self.records = [r for r in records if not r.get('archived')]
        self.context = QTableWidget(len(self.records), 3)
        self.context.setHorizontalHeaderLabels(['选择', '模块 / 名称', 'ID'])
        self.context.setEditTriggers(QTableWidget.NoEditTriggers)
        for i, record in enumerate(self.records):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            check.setCheckState(Qt.Unchecked)
            self.context.setItem(i, 0, check)
            self.context.setItem(i, 1, QTableWidgetItem(str(record.get('module', '')) + ' / ' + str(next(iter(record.get('data', {}).values()), ''))))
            self.context.setItem(i, 2, QTableWidgetItem(str(record.get('id', ''))))
        self.context.itemChanged.connect(self._limit_context)
        layout.addWidget(self.context)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet('color:#b42318')
        layout.addWidget(self.error)
        self.submit_button = QPushButton('预览完整外发内容、授权并生成')
        self.submit_button.clicked.connect(self.submit)
        layout.addWidget(self.submit_button)
        self.results = QTableWidget(0, 3)
        self.results.setHorizontalHeaderLabels(['字段（勾选填入）', '原值', '建议'])
        self.results.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.results)
        self.warnings = QPlainTextEdit()
        self.warnings.setReadOnly(True)
        self.warnings.setPlaceholderText('管理建议 / warnings：仅供参考，可选择复制，不自动创建任何记录。')
        layout.addWidget(self.warnings)
        self.apply_button = QPushButton('填入勾选字段（不保存）')
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_selected)
        layout.addWidget(self.apply_button)
        self.action_button=QPushButton('选择管理建议，建立正式任务…')
        self.action_button.setEnabled(False)
        self.action_button.clicked.connect(self.create_actions)
        layout.addWidget(self.action_button)
        self.call_id=None;self.suggestions=[]
        self.close_button = QPushButton('关闭')
        self.close_button.clicked.connect(self.reject)
        layout.addWidget(self.close_button)
        if not self.provider.count() or not self.prompt.count():
            self.submit_button.setEnabled(False)
            self.error.setText('没有可用模型或模板：请在系统设置配置并启用模型，在“系统设置→提示词库”新增/启用 purpose=form_fill 且 module=' + module + ' 的提示词，然后重新打开本窗口。')
        form.installEventFilter(self)
        form.set_busy(True)
        self.finished.connect(self._release)

    def eventFilter(self, watched, event):
        if watched is self.form and event.type() == QEvent.Close and not self._released:
            event.ignore()
            return True
        return super().eventFilter(watched, event)

    def _release(self, *_):
        if not self._released:
            self._released = True
            self.form.removeEventFilter(self)
            self.form.set_busy(self._parent_busy)

    def done(self, result):
        if not self.busy:
            super().done(result)

    def reject(self):
        if not self.busy:
            super().reject()

    def closeEvent(self, event):
        if self.busy:
            event.ignore()
        else:
            super().closeEvent(event)

    def selected_context(self):
        return [r for i, r in enumerate(self.records) if self.context.item(i, 0).checkState() == Qt.Checked]

    def _limit_context(self, item):
        if item.column() == 0 and len(self.selected_context()) > 30:
            item.setCheckState(Qt.Unchecked)
            self.error.setText('上下文最多选择30条。')

    def _set_busy(self, busy):
        self.busy = busy
        for widget in (self.basis, self.provider, self.prompt, self.context, self.results, self.close_button):
            widget.setEnabled(not busy)
        self.submit_button.setEnabled(not busy and bool(self.provider.count() and self.prompt.count()))
        self.apply_button.setEnabled(not busy and bool(self.fields))

    def preview(self, payload, records):
        preview = QDialog(self)
        preview.setWindowTitle('完整外发预览及费用授权')
        preview.resize(760, 640)
        layout = QVBoxLayout(preview)
        layout.addWidget(QLabel('以下基础输入、当前全部表单字段与所选上下文将发送至外部模型，可能产生费用。'))
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(json.dumps(dict(payload, context=records), ensure_ascii=False, indent=2))
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText('确认外发并承担模型费用')
        buttons.accepted.connect(preview.accept)
        buttons.rejected.connect(preview.reject)
        layout.addWidget(buttons)
        return preview.exec() == QDialog.Accepted

    def submit(self):
        if self.busy or _finalized(self.form):
            return
        if not self.provider.currentData() or not self.prompt.currentData():
            return
        records = self.selected_context()
        if len(records) > 30:
            self.error.setText('上下文最多选择30条。')
            return
        from .ai_consent import records_hash
        payload = dict(module=self.module, basis=self.basis.toPlainText(), current=self.form.values(),context_hash=records_hash(records),
                       context_ids=[r['id'] for r in records], provider_id=self.provider.currentData()['id'],
                       prompt_id=self.prompt.currentData()['id'], confirmed=True, sensitive_confirmed=False)
        self._set_busy(True)
        if not self.preview(payload, records):
            self._set_busy(False)
            return
        sensitive = self.module in SENSITIVE or any(r.get('module') in SENSITIVE for r in records)
        if sensitive:
            if not ask(self, '二次授权：当前表单或上下文含人员、考核或权限敏感资料，确认允许外发至模型？'):
                self._set_busy(False)
                return
            payload['sensitive_confirmed'] = True
        self.error.clear()
        try:
            self.runner.submit(lambda: self.client.post('/ai/form-fill', payload), self._completed, self._failed)
        except Exception as exc:
            self._failed(str(exc))

    def _failed(self, message):
        self._set_busy(False)
        self.error.setText('生成失败，输入已保留，可重试：' + str(message))

    def create_actions(self):
        if self.busy or not self.call_id:return
        from .management_ui import ActionCreationDialog
        dialog=ActionCreationDialog('ai:'+self.call_id,self.suggestions,self.client,self.runner,self)
        dialog.exec()

    def _completed(self, response):
        self.call_id=response.get('call_id') if isinstance(response,dict) else None
        self.suggestions=response.get('suggestions',[]) if isinstance(response,dict) else []
        self.action_button.setEnabled(bool(self.call_id and self.suggestions))
        fields = response.get('fields', {}) if isinstance(response, dict) else None
        if not isinstance(fields, dict):
            self._failed('返回的字段建议格式无效')
            return
        self.fields = fields
        current = self.form.values()
        self.results.setRowCount(len(fields))
        for i, (key, value) in enumerate(fields.items()):
            item = QTableWidgetItem(str(key))
            item.setData(Qt.UserRole, key)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if key in self.form.widgets and _empty(current.get(key)) and not _empty(value) else Qt.Unchecked)
            self.results.setItem(i, 0, item)
            self.results.setItem(i, 1, QTableWidgetItem(str(current.get(key, ''))))
            self.results.setItem(i, 2, QTableWidgetItem(str(value if value is not None else '')))
        details = {key: response.get(key) for key in ('suggestions', 'warnings', 'usage', 'model', 'call_id') if key in response}
        self.warnings.setPlainText('管理建议与调用信息（可复制，不会自动创建）：\n' + json.dumps(details, ensure_ascii=False, indent=2))
        self._set_busy(False)

    def apply_selected(self):
        if self.busy:
            return
        selected = [self.results.item(i, 0).data(Qt.UserRole) for i in range(self.results.rowCount())
                    if self.results.item(i, 0).checkState() == Qt.Checked]
        current = self.form.values()
        overwrite = any(not _empty(current.get(key)) for key in selected)
        if overwrite and not ask(self, '再次确认：勾选字段含已有值，将覆盖：' + '、'.join(k for k in selected if not _empty(current.get(k))) + '。仅填入，不保存，是否继续？'):
            return
        self.form.set_busy(False)
        try:
            result = apply_fields(self.form, self.fields, selected, overwrite=overwrite)
        finally:
            self.form.set_busy(True)
        self.error.setText('已填入（尚未保存）：' + '、'.join(result['applied']) + '\n跳过：' + json.dumps(result['skipped'], ensure_ascii=False))
        for i in range(self.results.rowCount()):
            self.results.item(i, 0).setCheckState(Qt.Unchecked)


def attach_ai_assistant(dialog, module, records, providers, prompts, runner, client):
    """Attach once to any RecordDialog; caller owns config and async transport."""
    if hasattr(dialog, 'ai_assistant_button'):
        return dialog.ai_assistant_button
    button = QPushButton('AI辅助填写…', dialog)
    button.setObjectName('ai_assistant_button')
    dialog.layout().insertWidget(0, button)
    dialog.ai_assistant_button = button
    button.setEnabled(not _finalized(dialog))
    if _finalized(dialog):
        button.setToolTip('已定稿记录禁止AI填写；请先按业务流程解除定稿。')

    def open_assistant():
        existing = getattr(dialog, 'ai_assistant', None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            return
        if _finalized(dialog) or getattr(dialog, 'busy', False):
            return
        assistant = AIFormDialog(dialog, module, records, providers, prompts, runner, client)
        dialog.ai_assistant = assistant
        assistant.show()
    button.clicked.connect(open_assistant)
    return button
