"""Daily checklist UI. Parent wires ChecklistsMixin and navigation key ``checklists``.

Requires page_layout, client, runner.submit(work, success, failure), current and
navigation generation. Drafts are memory-only; closing the application discards them.

Checklist templates are configurable. The list is read from GET /checklist-templates;
when that fails, is empty or is not a list, the built-in TEMPLATES are used so the page
still works offline. Template management only imports/exports declarative JSON and never
executes code.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from .checklist_templates import (
    DESCRIPTION_LIMIT, GENERIC_SCOPE, NAME_LIMIT, default_templates,
    export_template, item_names, scope_choices, template_names, validate_template,
)

CHECKLIST_NAV_KEY = 'checklists'
CHECKLIST_NAV_LABEL = '日常检查'
TEMPLATES = {
    '备份运行': ('备份作业运行结果', '备份覆盖范围与最近恢复点', '异地备份与恢复可用性'),
    '入离职权限': ('入职授权与审批依据', '离职账号停用及权限回收', '高权限账号与MFA复核'),
    '合同续费': ('合同到期与通知期限', '续费预算及审批进度', '供应商服务与退出安排'),
    '周度项目检查': ('里程碑与本周进展', '项目风险与阻塞', '下一步行动与责任人'),
}
RESULTS = ('待检查', '通过', '异常', '不适用')
TEMPLATE_RESULT_DEFAULT = '待检查'
_CHECKLIST_TEMPLATE_PATH = '/checklist-templates'
_REQUIRED_LABEL = '必填'


def _template_items(name, items=None):
    """(name, hint, required) triples for one template name or an explicit item list."""
    rows = deepcopy(items) if items else list(TEMPLATES.get(name, next(iter(TEMPLATES.values()))))
    result = []
    for row in rows:
        if isinstance(row, dict):
            result.append((str(row.get('name', '')), str(row.get('hint', '')), bool(row.get('required', False))))
        elif isinstance(row, (list, tuple)):
            result.append((str(row[0]), str(row[1]) if len(row) > 1 else '', bool(row[2]) if len(row) > 2 else False))
        else:
            result.append((str(row), '', False))
    return result


def _templates_from_api(value):
    """Template rows from the API, accepting both a flat list and a data envelope.

    The service stores ``{'data': {...}, 'id': ..., 'version': N}`` and returns it
    verbatim, so the GUI promotes id/version and keeps the stored config flat.
    """
    rows = value.get('templates', []) if isinstance(value, dict) else value
    if not isinstance(rows, list) or not rows:
        raise ValueError('检查模板响应无效，使用内置模板。')
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        config = row.get('data')
        if isinstance(config, dict):
            template = dict(config)
            if row.get('id') is not None:
                template.setdefault('id', row['id'])
            if row.get('version') is not None:
                template.setdefault('version', row['version'])
        elif isinstance(row.get('name'), str):
            template = row
        else:
            continue
        result.append(deepcopy(template))
    if not result:
        raise ValueError('检查模板响应无效，使用内置模板。')
    return result


def _fallback_templates():
    """Built-in templates as the same structures as configured ones."""
    try:
        return default_templates()
    except (ImportError, KeyError):  # pragma: no cover - defensive only
        return [{'name': name, 'description': '', 'scope': '', 'enabled': True, 'version': 1,
                 'items': [{'name': item, 'hint': '', 'required': False} for item in items]}
                for name, items in TEMPLATES.items()]


def _new_draft(name=None, owner='', items=None):
    name = name or next(iter(TEMPLATES))
    return {'name': name, 'owner': owner, 'date': QDate.currentDate().toString('yyyy-MM-dd'),
            'items': [{'name': item, 'result': TEMPLATE_RESULT_DEFAULT, 'note': ''}
                      for item, _, _ in _template_items(name, items)]}


def _runs(value):
    rows = value.get('runs', []) if isinstance(value, dict) else value
    if not isinstance(rows, list):
        raise ValueError('检查记录响应无效，请先核实服务状态。')
    return rows


def _run(value):
    return value.get('run', value) if isinstance(value, dict) else {}


class ChecklistTemplateDialog(QDialog):
    """FormDialog-compatible editor for one checklist template (values/validate/busy)."""

    FIELDS = ['name', 'description', 'scope', 'enabled', 'items']
    HEADERS = ['检查项', '通过判定 / 提示', _REQUIRED_LABEL]
    HINT = ('模板只是配置数据，不会执行任何代码；保存失败时输入会保留。')

    def __init__(self, template=None, title='编辑检查模板', parent=None, existing=None, allowed=None):
        super().__init__(parent)
        template = deepcopy(template) if isinstance(template, dict) else {}
        # Duplicate names are a list-level rule; the dialog enforces it against the live list.
        self.other_names = tuple(existing or ())
        self.allowed_names = tuple(allowed or ())
        self.setWindowTitle(title)
        self.resize(900, 660)
        self.busy = False
        self.validation_error = ''
        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setStyleSheet('font-size:19px;font-weight:600;')
        layout.addWidget(heading)
        note = QLabel(self.HINT)
        note.setWordWrap(True)
        layout.addWidget(note)
        basics = QFormLayout()
        self.name = QLineEdit(str(template.get('name', '')))
        self.name.setMaxLength(NAME_LIMIT)
        self.description = QPlainTextEdit(str(template.get('description', '')))
        self.description.setMaximumHeight(110)
        self.description.setPlaceholderText('说明与适用边界（可留空，最多%d字符）' % DESCRIPTION_LIMIT)
        self.scope = QComboBox()
        self.scope.setEditable(True)
        self.scope.setInsertPolicy(QComboBox.NoInsert)
        self.scope.addItem(GENERIC_SCOPE, '')
        for key, caption in scope_choices():
            if key:
                self.scope.addItem('%s（%s）' % (caption, key), key)
        scope = str(template.get('scope', ''))
        index = self.scope.findData(scope)
        if index >= 0:
            self.scope.setCurrentIndex(index)
        elif scope:
            self.scope.setEditText(scope)
        self.enabled = QCheckBox('启用（停用后仍保留历史检查记录）')
        self.enabled.setChecked(bool(template.get('enabled', True)))
        basics.addRow('模板名称 *', self.name)
        basics.addRow('模板说明', self.description)
        basics.addRow('适用范围', self.scope)
        basics.addRow('模板状态', self.enabled)
        layout.addLayout(basics)
        self.items = QTableWidget(0, len(self.HEADERS))
        self.items.setHorizontalHeaderLabels(self.HEADERS)
        self.items.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.items.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.items.setAlternatingRowColors(True)
        self.items.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.items.verticalHeader().hide()
        rows = template.get('items') if isinstance(template.get('items'), list) else []
        for row in rows:
            self._append_item(row if isinstance(row, dict) else {'name': str(row)})
        actions = QHBoxLayout()
        add = QPushButton('添加检查项')
        add.clicked.connect(lambda: self._append_item({}))
        actions.addWidget(add)
        remove = QPushButton('删除选中检查项')
        remove.clicked.connect(self._remove_selected)
        actions.addWidget(remove)
        actions.addWidget(QLabel('每行一项：名称 | 通过判定 / 提示 | 必填（1—50项）'))
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addWidget(self.items)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet('color:#b42318;background:#fff1f0;padding:8px;border-radius:4px;')
        self.error_label.hide()
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Save).setText('保存')
        self.buttons.button(QDialogButtonBox.Cancel).setText('取消')
        self.buttons.accepted.connect(self._accept_validated)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _append_item(self, item):
        row = self.items.rowCount()
        self.items.insertRow(row)
        self.items.setItem(row, 0, QTableWidgetItem(str(item.get('name', ''))))
        self.items.setItem(row, 1, QTableWidgetItem(str(item.get('hint', ''))))
        cell = QTableWidgetItem('是' if item.get('required') else '')
        cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
        self.items.setItem(row, 2, cell)

    def _remove_selected(self):
        rows = sorted({index.row() for index in self.items.selectedIndexes()}, reverse=True)
        if not rows and self.items.rowCount():
            rows = [self.items.rowCount() - 1]
        for row in rows:
            self.items.removeRow(row)

    def values(self):
        data = {
            'name': self.name.text().strip(),
            'description': self.description.toPlainText(),
            'scope': str(self.scope.currentData() or '') or (self.scope.currentText().strip() if self.scope.isEditable() else ''),
            'enabled': bool(self.enabled.isChecked()),
            'items': [],
        }
        for row in range(self.items.rowCount()):
            name = self.items.item(row, 0)
            hint = self.items.item(row, 1)
            required = self.items.item(row, 2)
            data['items'].append({
                'name': name.text().strip() if name else '',
                'hint': hint.text().strip() if hint else '',
                'required': bool(required and required.text().strip()),
            })
        return data

    def show_error(self, message, key=None):
        self.validation_error = str(message or '')
        self.error_label.setText(self.validation_error)
        self.error_label.setVisible(bool(self.validation_error))

    def validate(self):
        self.show_error('')
        data = self.values()
        try:
            validate_template(data, existing=self.other_names, allowed=self.allowed_names)
        except ValueError as exc:
            self.show_error(str(exc))
            return False
        return True

    def _accept_validated(self):
        if not self.busy and self.validate():
            self.accept()

    def set_busy(self, busy=True):
        self.busy = bool(busy)
        self.name.setEnabled(not self.busy)
        self.description.setEnabled(not self.busy)
        self.scope.setEnabled(not self.busy)
        self.enabled.setEnabled(not self.busy)
        self.items.setEnabled(not self.busy)
        self.buttons.setEnabled(not self.busy)
        self.buttons.button(QDialogButtonBox.Save).setText('保存中…' if self.busy else '保存')

    def reject(self):
        if self.busy:
            self.show_error('正在保存，请等待完成；当前输入已保留。')
            return
        super().reject()


class ChecklistsMixin:
    def checklists_page(self):
        self._checklist_page_serial = getattr(self, '_checklist_page_serial', 0) + 1
        self._checklist_template_serial = 0
        self._checklist_history_serial = 0
        self._checklist_page_alive = True
        # Templates survive page rebuilds; the API list only replaces them when it loads.
        self._checklist_templates = deepcopy(getattr(self, '_checklist_templates', _fallback_templates()))
        self._checklist_template_map = {row['name']: deepcopy(row) for row in self._checklist_templates}
        self._checklist_template_refs = dict(getattr(self, '_checklist_template_refs', {}))
        self._checklist_items_serial = getattr(self, '_checklist_items_serial', 0)
        self._checklist_filled_serial = -1
        if not getattr(self, 'checklist_draft', None):
            self.checklist_draft = _new_draft()
        draft = self.checklist_draft
        info = QLabel('逐项核实后选择结果，默认“待检查”不代表通过。异常必须填写说明；提交后自动建立风险与整改任务，已完成记录只读。草稿切页保留，但退出应用不会保存。')
        info.setWordWrap(True)
        self.page_layout.addWidget(info)
        # "新建检查" and the manager entry stay outside the read-only editor, so a
        # finished check never blocks starting the next one.
        self.checklist_new = QPushButton('新建检查')
        self.checklist_new.clicked.connect(self._checklist_new)
        self.checklist_manager_button = QPushButton('模板管理')
        # clicked passes its checked flag, so the slot must not receive it as "show".
        self.checklist_manager_button.clicked.connect(lambda: self._checklist_show_manager())
        self._checklist_bottom = QHBoxLayout()
        self._checklist_bottom.addWidget(self.checklist_new)
        self._checklist_bottom.addWidget(self.checklist_manager_button)
        self._checklist_bottom.addStretch(1)
        self.page_layout.addLayout(self._checklist_bottom)
        self.checklist_tabs = QStackedWidget()
        self.page_layout.addWidget(self.checklist_tabs)
        self.checklist_editor = QWidget()
        self.checklist_tabs.addWidget(self.checklist_editor)
        editor = QVBoxLayout(self.checklist_editor)
        editor.setContentsMargins(6, 6, 6, 6)
        self._checklist_building = True
        top = QHBoxLayout()
        self.checklist_template = QComboBox()
        self.checklist_date = QDateEdit(QDate.fromString(draft['date'], 'yyyy-MM-dd'))
        self.checklist_date.setCalendarPopup(True)
        self.checklist_date.setDisplayFormat('yyyy-MM-dd')
        self.checklist_owner = QLineEdit(draft['owner'])
        self.checklist_owner.setPlaceholderText('输入检查负责人（必填）')
        for label, widget in [('模板', self.checklist_template), ('日期', self.checklist_date), ('负责人', self.checklist_owner)]:
            top.addWidget(QLabel(label))
            top.addWidget(widget)
        editor.addLayout(top)
        self.checklist_items = self._checklist_table(['检查项', '结果', '备注 / 异常说明'])
        editor.addWidget(self.checklist_items)
        self.checklist_message = QLabel(getattr(self, '_checklist_notice', ''))
        self.checklist_message.setWordWrap(True)
        self.checklist_message.setTextFormat(Qt.PlainText)
        editor.addWidget(self.checklist_message)
        actions = QHBoxLayout()
        self.checklist_submit = QPushButton('确认并提交检查')
        self.checklist_submit.setProperty('primary', True)
        self.checklist_submit.clicked.connect(self._checklist_submit)
        actions.addWidget(self.checklist_submit)
        refresh = QPushButton('刷新历史记录')
        refresh.clicked.connect(self._checklist_load_runs)
        actions.addWidget(refresh)
        detail = QPushButton('查看只读详情')
        detail.clicked.connect(self._checklist_detail)
        actions.addWidget(detail)
        editor.addLayout(actions)
        self.checklist_history = self._checklist_table(['日期', '检查名称', '负责人', '异常数', '记录ID'])
        self.checklist_history.cellDoubleClicked.connect(lambda *_: self._checklist_detail())
        editor.addWidget(self.checklist_history)
        self._checklist_replace_templates(self._checklist_templates, keep_draft=True)
        self.checklist_owner.textChanged.connect(self._checklist_cache)
        self.checklist_date.dateChanged.connect(self._checklist_cache)
        self._checklist_building = False
        self._checklist_template_manage_page()
        self._checklist_update_controls()
        self._checklist_load_runs()
        self._checklist_load_templates()

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _checklist_table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(42)
        return table

    def _checklist_guard(self):
        generation, serial, client = self.generation, self._checklist_page_serial, self.client
        return lambda: (self.current == CHECKLIST_NAV_KEY and self.generation == generation
                        and self._checklist_page_serial == serial and self.client is client)

    def _checklist_widget_alive(self):
        return getattr(self, '_checklist_page_alive', True) and getattr(self, 'checklist_editor', None) is not None

    def _checklist_items_for(self, name):
        template = self._checklist_template_map.get(name)
        if template:
            return _template_items(name, template.get('items'))
        return _template_items(name, self.checklist_draft.get('items'))

    # ------------------------------------------------------- check execution tab

    def _checklist_fill_items(self):
        table = self.checklist_items
        table.setRowCount(0)
        table.setRowCount(len(self.checklist_draft['items']))
        for row, item in enumerate(self.checklist_draft['items']):
            table.setItem(row, 0, QTableWidgetItem(item['name']))
            result = QComboBox()
            result.addItems(RESULTS)
            result.setCurrentText(item['result'])
            note = QLineEdit(item['note'])
            note.setPlaceholderText('异常时必填：问题、影响或证据')
            table.setCellWidget(row, 1, result)
            table.setCellWidget(row, 2, note)
            result.currentTextChanged.connect(self._checklist_cache)
            note.textChanged.connect(self._checklist_cache)
        self._checklist_items_serial = getattr(self, '_checklist_items_serial', 0) + 1

    def _checklist_draft_in_table(self):
        """True when the draft originates from the widgets currently on the page."""
        return getattr(self, '_checklist_items_serial', 0) == getattr(self, '_checklist_filled_serial', -1)

    def _checklist_cache(self, *_):
        if self._checklist_building:
            return
        value = {'name': self.checklist_template.currentText(), 'owner': self.checklist_owner.text(),
                 'date': self.checklist_date.date().toString('yyyy-MM-dd'), 'items': []}
        for row in range(self.checklist_items.rowCount()):
            value['items'].append({'name': self.checklist_items.item(row, 0).text(),
                                   'result': self.checklist_items.cellWidget(row, 1).currentText(),
                                   'note': self.checklist_items.cellWidget(row, 2).text()})
        if value != self.checklist_draft:
            self.checklist_draft = value
            self._checklist_attempt = None  # Any content change starts a new UUID.
            self._checklist_notice = ''
            self.checklist_message.clear()
            self._checklist_filled_serial = getattr(self, '_checklist_items_serial', 0)
        self._checklist_update_controls()

    def _checklist_change_template(self, name):
        if self._checklist_building:
            return
        self._checklist_sync_items(name)

    def _checklist_sync_items(self, name):
        """Keep the draft in step with a template; never discard answers unseen."""
        if not name or getattr(self, 'checklist_items', None) is None:
            return
        draft = self.checklist_draft
        previous = [dict(item) for item in draft.get('items', [])]
        names = self._checklist_item_names(name)
        if not names:
            self._checklist_update_controls()
            return
        same_items = [item['name'] for item in previous] == names
        if draft.get('name') == name and same_items and self._checklist_draft_in_table():
            self._checklist_update_controls()
            return
        self._checklist_building = True
        if draft.get('name') == name and same_items:
            # The same template was merely reloaded: keep every answer and the request ID.
            self.checklist_draft = dict(draft, date=self.checklist_date.date().toString('yyyy-MM-dd'))
        else:
            self.checklist_draft = _new_draft(name, self.checklist_owner.text())
            self.checklist_draft['date'] = self.checklist_date.date().toString('yyyy-MM-dd')
            self._checklist_attempt = None
            old = {item['name']: item for item in previous}
            self.checklist_draft['items'] = [
                {'name': item, 'result': old.get(item, {}).get('result', TEMPLATE_RESULT_DEFAULT),
                 'note': old.get(item, {}).get('note', '')}
                for item, _, _ in _template_items(name, self._checklist_templates_for(name))]
        self._checklist_fill_items()
        if not (draft.get('name') == name and same_items):
            self._checklist_notice = ''
            if getattr(self, 'checklist_message', None) is not None:
                self.checklist_message.clear()
        self._checklist_building = False
        self._checklist_filled_serial = getattr(self, '_checklist_items_serial', 0)
        self._checklist_update_controls()

    def _checklist_templates_for(self, name):
        template = self._checklist_template_map.get(name)
        return template.get('items') if template else None

    def _checklist_item_names(self, name):
        """Item names currently offered for a template name, from the loaded list."""
        template = self._checklist_template_map.get(name)
        if template:
            return item_names(template)
        return [item for item, _, _ in _template_items(name, self.checklist_draft.get('items'))]

    def _checklist_valid(self):
        draft = self.checklist_draft
        return bool(draft['owner'].strip() and draft['items'] and all(
            item['result'] in RESULTS[1:] and (item['result'] != '异常' or item['note'].strip())
            for item in draft['items']))

    def _checklist_release_locked_widgets(self):
        """Re-enable the tree even when Qt left it force-disabled while off screen."""
        self.checklist_editor.setAttribute(Qt.WA_ForceDisabled, False)
        for widget in self.checklist_editor.findChildren(QWidget):
            if widget.testAttribute(Qt.WA_ForceDisabled):
                widget.setAttribute(Qt.WA_ForceDisabled, False)
                widget.setEnabled(True)

    def _checklist_update_controls(self):
        attempt = getattr(self, '_checklist_attempt', None) or {}
        busy, completed = attempt.get('busy', False), attempt.get('completed', False)
        locked = busy or completed
        # Qt may not propagate "enabled" back down onto a widget tree that was disabled
        # while this page was off screen, so every control gets its own explicit state.
        if not locked:
            self._checklist_release_locked_widgets()
        self.checklist_editor.setEnabled(not locked)
        self.checklist_owner.setEnabled(not locked)
        self.checklist_date.setEnabled(not locked)
        self.checklist_template.setEnabled(not locked)
        self.checklist_items.setEnabled(not locked)
        self.checklist_submit.setEnabled(self._checklist_valid() and not locked)
        self.checklist_submit.setText('先核对记录，再重试' if attempt.get('uncertain') else '确认并提交检查')
        # "新建检查" lives outside the read-only editor, so a finished check stays reachable.
        self.checklist_new.setEnabled(not busy)

    def _checklist_new(self):
        if QMessageBox.question(self, '新建检查', '放弃当前草稿并开始新检查？若上次结果不确定，请先核对历史记录。',
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        name, owner = self.checklist_template.currentText(), self.checklist_owner.text()
        self._checklist_building = True
        self.checklist_draft = _new_draft(name, owner)
        self._checklist_attempt = None
        self._checklist_notice = ''
        self.checklist_message.clear()
        self.checklist_date.setDate(QDate.currentDate())
        self._checklist_fill_items()
        self._checklist_filled_serial = self._checklist_items_serial
        self._checklist_building = False
        self._checklist_update_controls()

    def _checklist_submit(self):
        if not self._checklist_valid():
            self.checklist_message.setText('请填写负责人，完成全部检查项，并为每项异常填写说明。')
            return
        attempt = getattr(self, '_checklist_attempt', None)
        if attempt and (attempt.get('busy') or attempt.get('completed')):
            return
        count = sum(item['result'] == '异常' for item in self.checklist_draft['items'])
        if QMessageBox.question(self, '确认完成检查',
                                f'将为 {count} 项异常建立 {count} 条风险与 {count} 条整改任务。\n提交后记录只读，是否继续？',
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        if not attempt:
            attempt = {'payload': dict(deepcopy(self.checklist_draft), request_id=str(uuid4()))}
            self._checklist_attempt = attempt
        check_first = attempt.get('uncertain', False)
        attempt.update(busy=True, uncertain=True)
        self._checklist_update_controls()
        active, client = self._checklist_guard(), self.client
        payload = deepcopy(attempt['payload'])

        def work():
            if check_first:
                # If lookup fails, do NOT send another POST; the outcome remains unknown.
                for run in _runs(client.get('/checklists/runs')):
                    if run.get('request_id') == payload['request_id']:
                        return run
            return client.post('/checklists/complete', payload)

        def success(value):
            attempt.update(busy=False, uncertain=False, completed=True)
            run = _run(value)
            ids = {}
            for key, records_key in (('task_ids', 'tasks'), ('risk_ids', 'risks')):
                records = value.get(records_key, []) if isinstance(value, dict) else []
                ids[key] = run.get(key, [record['id'] for record in records if isinstance(record, dict) and 'id' in record])
            notice = ('检查已完成（只读）。记录ID：' + str(run.get('id', '')) +
                      '\n任务 IDs：' + json.dumps(ids['task_ids'], ensure_ascii=False) +
                      '\n风险 IDs：' + json.dumps(ids['risk_ids'], ensure_ascii=False))
            if getattr(self, '_checklist_attempt', None) is attempt and self.client is client:
                self._checklist_notice = notice
            if not (self.current == CHECKLIST_NAV_KEY and self.client is client
                    and getattr(self, '_checklist_attempt', None) is attempt):
                return
            self.checklist_message.setText(notice)
            self._checklist_update_controls()
            self._checklist_load_runs()

        def failure(message):
            attempt['busy'] = False
            if self.client is client and getattr(self, '_checklist_attempt', None) is attempt:
                self._checklist_notice = str(message) + '\n提交结果不确定。重试将先查询记录；内容不变时沿用同一请求ID。'
                if self.current == CHECKLIST_NAV_KEY:
                    self.checklist_message.setText(self._checklist_notice)
                    self._checklist_update_controls()
        self.runner.submit(work, success, failure)

    def _checklist_load_runs(self):
        active, client = self._checklist_guard(), self.client
        self._checklist_history_serial = getattr(self, '_checklist_history_serial', 0) + 1
        serial = self._checklist_history_serial

        def success(rows):
            if not active() or not self._checklist_widget_alive() or serial != self._checklist_history_serial:
                return
            table = self.checklist_history
            table.setRowCount(len(rows))
            refs = {}
            for index, run in enumerate(rows):
                count = run.get('exception_count', sum(i.get('result') == '异常' for i in run.get('items', [])))
                for key in (run.get('template_id'), run.get('template_name'), run.get('name')):
                    if key:
                        refs[key] = refs.get(key, 0) + 1
                for column, text in enumerate((run.get('date', ''), run.get('name', ''), run.get('owner', ''), count, run.get('id', ''))):
                    cell = QTableWidgetItem(str(text))
                    cell.setData(Qt.UserRole, run)
                    table.setItem(index, column, cell)
            self._checklist_template_refs = refs
            self._checklist_fill_template_table()

        def failure(message):
            if active() and self._checklist_widget_alive() and serial == self._checklist_history_serial:
                self.checklist_message.setText('历史记录加载失败：' + str(message))
        self.runner.submit(lambda: _runs(client.get('/checklists/runs')), success, failure)

    def _checklist_detail(self):
        cell = self.checklist_history.item(self.checklist_history.currentRow(), 0)
        if cell is None:
            self.checklist_message.setText('请先选择一条历史检查记录。')
            return
        run = cell.data(Qt.UserRole)
        active, client = self._checklist_guard(), self.client

        def show(value):
            if not active() or not self._checklist_widget_alive():
                return
            dialog = QDialog(self)
            dialog.setWindowTitle('已完成检查 · 只读详情（问题及关联ID）')
            dialog.resize(760, 580)
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel('历史记录不可修改。items 中可查看异常说明及关联 task / risk IDs。'))
            text = QPlainTextEdit()
            text.setReadOnly(True)
            text.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))
            layout.addWidget(text)
            buttons = QDialogButtonBox(QDialogButtonBox.Close)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            dialog.exec()

        def failure(message):
            if active() and self._checklist_widget_alive():
                self.checklist_message.setText('详情加载失败：' + str(message))
        self.runner.submit(lambda: client.get('/checklists/runs/' + quote(str(run['id']), safe='')), show, failure)

    # -------------------------------------------------------- template loading

    def _checklist_load_templates(self):
        active, client = self._checklist_guard(), self.client
        self._checklist_template_serial = getattr(self, '_checklist_template_serial', 0) + 1
        serial = self._checklist_template_serial

        def success(value):
            if not active() or not self._checklist_widget_alive() or serial != self._checklist_template_serial:
                return
            try:
                rows = _templates_from_api(value)
            except ValueError:
                self._checklist_replace_templates(_fallback_templates(), keep_draft=True)
                return
            self._checklist_replace_templates(rows, keep_draft=True)
            self._checklist_fill_template_table()

        def failure(message):
            if not active() or not self._checklist_widget_alive() or serial != self._checklist_template_serial:
                return
            self._checklist_replace_templates(_fallback_templates(), keep_draft=True)
            self._checklist_fill_template_table()
        self.runner.submit(lambda: client.get(_CHECKLIST_TEMPLATE_PATH), success, failure)

    def _checklist_replace_templates(self, rows, keep_draft=False):
        """Publish a template list to the dropdown, keeping the draft and its answers."""
        usable = [row for row in (rows or []) if isinstance(row, dict)
                  and isinstance(row.get('name'), str) and row.get('name')]
        self._checklist_templates = deepcopy(usable)
        self._checklist_template_map = {row['name']: deepcopy(row) for row in self._checklist_templates}
        names = template_names(self._checklist_templates)
        draft_name = (self.checklist_draft or {}).get('name')
        if keep_draft and draft_name and draft_name not in names:
            names = names + [draft_name]
        # The configured order wins, so the dropdown never leads with a template the
        # server does not offer; the draft's own answers are still preserved below.
        wanted = names[0] if names else ''
        self._checklist_building = True
        combo = self.checklist_template
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(names)
        if wanted:
            combo.setCurrentText(wanted)
        combo.blockSignals(False)
        self._checklist_building = False
        self._checklist_sync_items(combo.currentText())
        self._checklist_fill_template_table()

    # ----------------------------------------------------- template management

    def _checklist_template_manage_page(self):
        self.checklist_template_panel = QWidget()
        self.checklist_tabs.addWidget(self.checklist_template_panel)
        layout = QVBoxLayout(self.checklist_template_panel)
        hint = QLabel('模板是配置数据：可新建、编辑、复制、停用、删除与导入导出，导入前先预览且不执行任何代码。停用或删除不会改动历史检查记录。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QHBoxLayout()
        entries = (
            ('新建模板', lambda: self._checklist_template_edit(new=True)),
            ('编辑模板', self._checklist_template_edit),
            ('复制模板', lambda: self._checklist_template_edit(copy=True)),
            ('启用 / 停用', self._checklist_template_toggle),
            ('删除模板', self._checklist_template_delete),
            ('导入JSON（先预览）', self._checklist_template_import),
            ('导出配置到文件', self._checklist_template_export_file),
            ('复制配置到剪贴板', self._checklist_template_copy),
            ('刷新模板', self._checklist_load_templates),
        )
        for text, callback in entries:
            button = QPushButton(text)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        back = QPushButton('返回检查执行')
        back.clicked.connect(lambda: self._checklist_show_manager(False))
        buttons.addWidget(back)
        layout.addLayout(buttons)
        self.checklist_template_table = self._checklist_table(
            ['模板名称', '说明', '项目数', '启用', '版本', 'ID', '引用检查记录'])
        self.checklist_template_table.cellDoubleClicked.connect(lambda *_: self._checklist_template_edit())
        layout.addWidget(self.checklist_template_table)
        self.checklist_template_message = QLabel()
        self.checklist_template_message.setWordWrap(True)
        layout.addWidget(self.checklist_template_message)
        self._checklist_fill_template_table()

    def _checklist_show_manager(self, show=True):
        """Switch between the check tab and the template manager in place."""
        self.checklist_tabs.setCurrentIndex(1 if show else 0)
        if show:
            self._checklist_load_templates()

    def _checklist_fill_template_table(self, referenced=None):
        table = getattr(self, 'checklist_template_table', None)
        if table is None:
            return
        rows = getattr(self, '_checklist_templates', [])
        counts = referenced if referenced is not None else getattr(self, '_checklist_template_refs', {})
        table.setRowCount(len(rows))
        for index, template in enumerate(rows):
            name = template.get('name', '')
            values = (
                name,
                template.get('description', ''),
                len([item for item in template.get('items', []) if isinstance(item, dict)]),
                '启用' if template.get('enabled', True) else '停用',
                template.get('version', ''),
                template.get('id', ''),
                counts.get(template.get('id'), 0) or counts.get(name, 0),
            )
            for column, text in enumerate(values):
                cell = QTableWidgetItem(str(text))
                cell.setData(Qt.UserRole, deepcopy(template))
                table.setItem(index, column, cell)

    def _checklist_template_row(self):
        table = getattr(self, 'checklist_template_table', None)
        if table is None:
            return None
        item = table.item(table.currentRow(), 0)
        return item.data(Qt.UserRole) if item else None

    def _checklist_template_set_message(self, text):
        label = getattr(self, 'checklist_template_message', None)
        if label is not None:
            label.setText(str(text))

    def _checklist_template_save(self, dialog, template, oid=None, version=None):
        """Save with a frozen form; failures keep the input, success refreshes.

        ``template`` may be a callable returning the current form input, so a retry
        always submits exactly what the form shows.
        """
        valid, client = self._checklist_guard(), self.client
        alive = [True]
        dialog.finished.connect(lambda *_: alive.__setitem__(0, False))
        dialog.buttons.accepted.disconnect()

        def payload():
            data = deepcopy(template() if callable(template) else template)
            data.pop('id', None)
            body = {'data': data}
            if oid is not None:
                body['id'] = oid
            if version is not None:
                body['version'] = version
            return body

        def save():
            if dialog.busy or not alive[0]:
                return
            if self.client is not client:
                dialog.show_error('工作库已切换，请关闭此窗口后重新打开。')
                return
            if not dialog.validate():
                return
            body = payload()
            dialog.set_busy(True)
            self._checklist_template_set_message('正在保存模板配置…')
            template_panel = getattr(self, 'checklist_template_panel', None)
            if template_panel is not None:
                # Freeze the manager page too, not only the editor dialog.
                template_panel.setEnabled(False)

            def done(value):
                if not alive[0]:
                    return
                if template_panel is not None:
                    template_panel.setEnabled(True)
                dialog.set_busy(False)
                dialog.accept()
                if valid() and self._checklist_widget_alive():
                    self._checklist_template_set_message('模板已保存为新版本：' + str(body['data'].get('name', '')))
                    self._checklist_load_templates()
                elif self._checklist_widget_alive():
                    # The page was replaced; refresh the live page, never this dialog's widgets.
                    self.checklist_template_message.setText('模板已保存。')

            def failed(message):
                if not alive[0]:
                    return
                if template_panel is not None:
                    template_panel.setEnabled(True)
                dialog.set_busy(False)
                dialog.show_error(str(message))
            self.runner.submit(lambda: client.post(_CHECKLIST_TEMPLATE_PATH, body), done, failed)

        dialog.buttons.accepted.connect(save)
        return dialog

    def _checklist_template_open(self, template, title, oid=None, version=None, allowed=()):
        dialog = ChecklistTemplateDialog(template, title, self, existing=template_names(self._checklist_templates),
                                         allowed=allowed)
        # Keep the editors reachable so a closed dialog is never treated as still saving.
        self._checklist_template_dialogs = (getattr(self, '_checklist_template_dialogs', []) + [dialog])[-4:]
        self._checklist_template_save(dialog, dialog.values, oid, version)
        dialog.exec()
        return dialog

    def _checklist_template_edit(self, checked=False, copy=False, new=False):
        row = None if new else self._checklist_template_row()
        if row is None and not new:
            self._checklist_template_set_message('请先选择一行模板。')
            return None
        allowed = ()
        if row is None:
            seed = next(iter(self._checklist_template_map.values()), None) or self._checklist_templates[0]
            template = {'name': '', 'description': '', 'scope': '', 'enabled': True,
                        'items': [{'name': item, 'hint': hint, 'required': required}
                                  for item, hint, required in _template_items(seed.get('name'), seed.get('items'))]}
            title = '新建检查模板'
        else:
            template = deepcopy(row)
            title = '复制检查模板（保存为新模板）' if copy else '编辑检查模板'
            allowed = (row.get('name'),)
        if copy:
            template['name'] = str(template.get('name', '')) + '（副本）'
            template.pop('version', None)
            template.pop('id', None)
        oid = None if (new or copy) else row.get('id')
        version = None if (new or copy) else row.get('version')
        return self._checklist_template_open(template, title, oid, version, allowed)

    def _checklist_template_toggle(self):
        row = self._checklist_template_row()
        if not row:
            self._checklist_template_set_message('请先选择一行模板。')
            return
        template = deepcopy(row)
        template.pop('id', None)
        template['enabled'] = not bool(template.get('enabled', True))
        self._checklist_template_open(template, '启用 / 停用检查模板', row.get('id'), row.get('version'),
                                     (row.get('name'),))

    def _checklist_template_delete(self):
        row = self._checklist_template_row()
        if not row:
            self._checklist_template_set_message('请先选择一行模板。')
            return
        refs = getattr(self, '_checklist_template_refs', {})
        referenced = refs.get(row.get('id'), 0) or refs.get(row.get('name'), 0)
        if referenced:
            if QMessageBox.question(self, '模板已被引用',
                                    '该模板已被 %d 条历史检查记录引用，删除会使历史记录的模板名无法追溯。\n改为停用（保留历史、不再出现在新建检查中）？' % referenced,
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes:
                return
            template = deepcopy(row)
            template.pop('id', None)
            template['enabled'] = False
            self._checklist_template_open(template, '停用已被引用的检查模板', row.get('id'), row.get('version'),
                                         (row.get('name'),))
            return
        if QMessageBox.question(self, '删除检查模板',
                                '删除模板“%s”？未保存的检查草稿不受影响；此操作不需要代码执行。' % row.get('name', ''),
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._checklist_template_delete_request(row)

    def _checklist_template_delete_request(self, row):
        valid, client = self._checklist_guard(), self.client
        oid, version = row.get('id'), row.get('version')
        if oid is None:
            self._checklist_template_set_message('内置模板没有服务端ID，可停用但不能删除。')
            return
        self._checklist_template_set_message('正在删除模板…')

        def done(value):
            if not valid() or not self._checklist_widget_alive():
                return
            self._checklist_template_set_message('模板已删除：' + str(row.get('name', '')))
            self._checklist_load_templates()

        def failed(message):
            if valid() and self._checklist_widget_alive():
                self._checklist_template_set_message('删除失败：' + str(message))
        self.runner.submit(lambda: client.post(_CHECKLIST_TEMPLATE_PATH + '/delete',
                                               {'id': oid, 'version': version}), done, failed)

    def _checklist_template_import(self):
        filename, _ = QFileDialog.getOpenFileName(self, '导入检查模板JSON', '', 'JSON (*.json)')
        if not filename:
            return
        try:
            value = json.loads(Path(filename).read_text(encoding='utf-8-sig'))
        except (OSError, ValueError, TypeError) as exc:
            self._checklist_template_set_message('导入失败：' + str(exc))
            return
        config = value.get('config', value) if isinstance(value, dict) else None
        if not isinstance(config, dict):
            self._checklist_template_set_message('导入失败：需要包含模板配置的JSON对象。')
            return
        template = deepcopy(config)
        template.pop('id', None)
        template.pop('version', None)
        template.setdefault('enabled', True)
        template.setdefault('scope', '')
        template.setdefault('description', '')
        if not isinstance(template.get('items'), list):
            template['items'] = []
        # Preview first: nothing is sent and no code is executed.
        self._checklist_template_open(template, '导入预览：核对后保存为新模板（不执行代码）')

    @staticmethod
    def _checklist_template_file_payload(template):
        value = export_template(template)
        value['version'] = template.get('version')
        return value

    def _checklist_template_export_file(self):
        row = self._checklist_template_row()
        if not row:
            self._checklist_template_set_message('请先选择一行模板。')
            return
        value = self._checklist_template_file_payload(row)
        filename, _ = QFileDialog.getSaveFileName(self, '导出检查模板配置JSON', str(value.get('name', 'template')) + '.json', 'JSON (*.json)')
        if not filename:
            return
        try:
            Path(filename).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as exc:
            self._checklist_template_set_message('导出失败：' + str(exc))
            return
        self._checklist_template_set_message('已导出模板配置（不含内部ID与历史）：' + filename)

    def _checklist_template_copy(self):
        row = self._checklist_template_row()
        if not row:
            self._checklist_template_set_message('请先选择一行模板。')
            return
        value = self._checklist_template_file_payload(row)
        try:
            QApplication.clipboard().setText(json.dumps(value, ensure_ascii=False, indent=2))
        except AttributeError:  # pragma: no cover - clipboard unavailable offscreen
            self._checklist_template_set_message('剪贴板不可用，请改用“导出配置到文件”。')
            return
        self._checklist_template_set_message('模板配置已复制到剪贴板（仅配置，不含内部ID）。')
