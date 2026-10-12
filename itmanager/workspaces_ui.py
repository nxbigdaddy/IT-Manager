"""Special-purpose workspaces. MainWindow wires WorkspaceSystemsMixin/workspaces.

Only declarative JSON is imported. Existing workspaces always use their snapshot.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from .forms import FormDialog

WORKSPACES_NAV_KEY = 'workspaces'
WORKSPACES_NAV_LABEL = '专项工作空间'
IPO_BOUNDARY = 'IPO专项仅用于IT准备、证据梳理及整改跟踪，不构成审计、法律意见或上市合规保证；须由专业机构独立复核。'


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def _rows(value, key):
    rows = value.get(key, []) if isinstance(value, dict) else value
    if not isinstance(rows, list):
        raise ValueError('服务端列表格式无效：' + key)
    return rows


def _button(layout, text, callback):
    button = QPushButton(text)
    button.clicked.connect(callback)
    layout.addWidget(button)
    return button


def _table(headers):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _fill(table, rows, keys):
    table.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, key in enumerate(keys):
            value = key(row) if callable(key) else row.get(key, '')
            item = QTableWidgetItem(str(value))
            item.setData(Qt.UserRole, deepcopy(row))
            table.setItem(i, j, item)


def _selected(table):
    item = table.item(table.currentRow(), 0)
    return item.data(Qt.UserRole) if item else None


def entry_fields(form):
    """Translate snapshot schema, never the subsequently edited template."""
    fields, choices = [], {}
    for field in form.get('fields', []):
        key = field['key']
        kind = field.get('type', 'text')
        if kind not in ('text', 'long', 'date', 'choice'):
            raise ValueError('不支持的字段类型：' + str(kind))
        if kind == 'choice':
            choices[key] = [(str(v.get('label', v.get('value', ''))), v.get('value', v.get('key', '')))
                            if isinstance(v, dict) else (str(v), v) for v in field.get('options', [])]
        fields.append((key, field.get('label', key) + (' *' if field.get('required') else ''), kind, ''))
    return fields, choices


class TemplateEditor(QDialog):
    """Structured editor preserves unknown/advanced keys without executing them."""
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle('配置式模板编辑（保存为新版本，不改已有工作空间）')
        self.resize(920, 650)
        self.original = deepcopy(config)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)
        basic = QWidget(); basic_layout = QFormLayout(basic)
        self.name = QLineEdit(str(config.get('name', '')))
        self.description = QPlainTextEdit(str(config.get('description', '')))
        basic_layout.addRow('模板名称 *', self.name)
        basic_layout.addRow('说明与适用边界', self.description)
        tabs.addTab(basic, '名称与说明')
        self.tables = {}
        for key, title, columns in [('phases', '阶段', ['key', 'name', 'deliverables', 'checklist']),
                                     ('work_packages', '工作包', ['name', 'phase', 'acceptance'])]:
            page = QWidget(); box = QVBoxLayout(page)
            table = QTableWidget(0, len(columns)); table.setHorizontalHeaderLabels(columns)
            table.horizontalHeader().setStretchLastSection(True)
            values = deepcopy(config.get(key, []))
            table.setRowCount(len(values))
            for i, value in enumerate(values):
                if not isinstance(value, dict):
                    value = {'name': str(value)}
                for j, column in enumerate(columns):
                    cell = value.get(column, '')
                    table.setItem(i, j, QTableWidgetItem(_json(cell) if isinstance(cell, (list, dict)) else str(cell)))
            box.addWidget(QLabel('交付物/检查清单可逐行输入，也可输入JSON数组；其他高级属性会保留。'))
            box.addWidget(table)
            actions = QHBoxLayout(); box.addLayout(actions)
            _button(actions, '添加行', lambda checked=False, t=table: t.insertRow(t.rowCount()))
            _button(actions, '删除选中行', lambda checked=False, t=table: t.removeRow(t.currentRow()))
            # Original metadata travels with each row through deletion.
            for i, value in enumerate(values):
                table.item(i, 0).setData(Qt.UserRole, value)
            self.tables[key] = (table, columns)
            tabs.addTab(page, title)
        advanced = QWidget(); advanced_layout = QVBoxLayout(advanced)
        advanced_layout.addWidget(QLabel('表单、提示词、报告章节及其他高级配置（仅JSON数据，不执行代码）'))
        self.advanced = QPlainTextEdit(_json({k: v for k, v in config.items() if k not in ('name', 'description', 'phases', 'work_packages')}))
        advanced_layout.addWidget(self.advanced); tabs.addTab(advanced, '高级JSON')
        self.error = QLabel(); layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def values(self):
        result = json.loads(self.advanced.toPlainText())
        if not isinstance(result, dict):
            raise ValueError('高级配置必须是JSON对象')
        if not self.name.text().strip():
            raise ValueError('请填写模板名称')
        result.update(name=self.name.text().strip(), description=self.description.toPlainText())
        for key, (table, columns) in self.tables.items():
            result[key] = []
            for i in range(table.rowCount()):
                first = table.item(i, 0)
                row = deepcopy(first.data(Qt.UserRole) or {}) if first else {}
                if not isinstance(row, dict): row = {}
                for j, column in enumerate(columns):
                    item = table.item(i, j); value = item.text().strip() if item else ''
                    if column in ('deliverables', 'checklist'):
                        value = json.loads(value) if value.startswith('[') else [x.strip() for x in value.splitlines() if x.strip()]
                    row[column] = value
                result[key].append(row)
        return result

    def _save(self):
        try:
            self.values()
        except (ValueError, TypeError) as exc:
            self.error.setText(str(exc)); return
        self.accept()


class WorkspaceSystemsMixin:
    def _ws_guard(self):
        client, generation, serial = self.client, self.generation, self._ws_serial
        return lambda: (self.client is client and self.generation == generation and
                        self.current == WORKSPACES_NAV_KEY and self._ws_serial == serial)

    def _ws_request(self, method, path, payload, success, dialog=None, button=None):
        client, valid = self.client, self._ws_guard()
        if button: button.setEnabled(False)
        alive = [True]
        if dialog: dialog.finished.connect(lambda *_: alive.__setitem__(0, False))
        def usable(): return valid() and alive[0]
        def done(value):
            if usable():
                if button: button.setEnabled(True)
                success(value)
        def failed(message):
            if usable():
                if button: button.setEnabled(True)
                self.error(str(message))
        data = deepcopy(payload)
        self.runner.submit(lambda: client.get(path) if method == 'GET' else client.post(path, data), done, failed)

    def workspaces_page(self):
        self._ws_serial = getattr(self, '_ws_serial', 0) + 1
        self.ws_templates = []
        tabs = QTabWidget(); self.page_layout.addWidget(tabs)
        work = QWidget(); layout = QVBoxLayout(work); tabs.addTab(work, '专项工作空间')
        boundary = QLabel(IPO_BOUNDARY); boundary.setWordWrap(True); layout.addWidget(boundary)
        actions = QHBoxLayout(); layout.addLayout(actions)
        self.ws_create_button = _button(actions, '新建专项工作空间', self._ws_create)
        self.ws_create_button.setEnabled(False)
        _button(actions, '进入工作空间', self._ws_open)
        _button(actions, '刷新', self._ws_load)
        self.ws_table = _table(['名称', '负责人', '开始日期', '截止日期', '项目ID'])
        self.ws_table.cellDoubleClicked.connect(lambda *_: self._ws_open()); layout.addWidget(self.ws_table)
        templates = QWidget(); box = QVBoxLayout(templates); tabs.addTab(templates, '配置式模板')
        hint = QLabel('内置模板与自定义模板均为配置数据。复制后定制；修改模板仅影响以后新建的工作空间。' + IPO_BOUNDARY)
        hint.setWordWrap(True); box.addWidget(hint)
        actions = QHBoxLayout(); box.addLayout(actions)
        _button(actions, '新建模板', lambda: self._ws_template_edit(new=True))
        _button(actions, '复制模板', lambda: self._ws_template_edit(copy=True))
        _button(actions, '编辑模板', self._ws_template_edit)
        _button(actions, '导入JSON', self._ws_import)
        _button(actions, '导出JSON', self._ws_export)
        self.ws_template_table = _table(['模板名称', '说明 / 边界', '版本', 'ID']); box.addWidget(self.ws_template_table)
        self._ws_load()

    def _ws_load(self):
        self._ws_request('GET', '/workspaces', None,
                         lambda value: _fill(self.ws_table, _rows(value, 'workspaces'), ['name', 'owner', 'start_date', 'due_date', 'project_id']))
        def templates(value):
            self.ws_templates = deepcopy(_rows(value, 'templates'))
            _fill(self.ws_template_table, self.ws_templates,
                  [lambda r: r.get('config', {}).get('name', r.get('id', '')), lambda r: r.get('config', {}).get('description', ''), 'version', 'id'])
            self.ws_create_button.setEnabled(bool(self.ws_templates))
        self._ws_request('GET', '/workspace-templates', None, templates)

    def _ws_creation_payload(self, data):
        """Keep the request UUID after ambiguous failure, including reopening editor."""
        data = deepcopy(data)
        previous = getattr(self, '_ws_create_attempt', None)
        if not previous or previous[0] != data:
            self._ws_create_attempt = (deepcopy(data), str(uuid4()))
        return dict(data, request_id=self._ws_create_attempt[1])

    def _ws_create(self):
        if not self.ws_templates: return
        fields = [('template_id', '专项模板 *', 'choice', ''), ('name', '工作空间名称 *', 'text', ''),
                  ('owner', '负责人 *', 'text', ''), ('start_date', '开始日期 *', 'date', ''), ('due_date', '截止日期 *', 'date', '')]
        previous = getattr(self, '_ws_create_attempt', None)
        dialog = FormDialog('新建专项工作空间', fields, previous[0] if previous else {},
                            {'template_id': [(r['config']['name'], r['id']) for r in self.ws_templates]}, self)
        self._ws_bind_save(dialog, lambda data: self._ws_creation_payload(data), '/workspaces',
                           lambda _: (setattr(self, '_ws_create_attempt', None), self._ws_load()))
        dialog.exec()

    def _ws_bind_save(self, dialog, payload, path, success):
        # A modal editor owns its request lifetime. A delayed main-page refresh
        # must not silently invalidate an open editor or discard its save result.
        valid, client = self._ws_guard(), self.client
        alive = [True]
        dialog.finished.connect(lambda *_: alive.__setitem__(0, False))
        dialog.buttons.accepted.disconnect()
        def save():
            if dialog.busy or not alive[0]: return
            if self.client is not client:
                dialog.show_error('工作库已切换，请关闭此窗口后重新打开。'); return
            if not dialog.validate(): return
            values = dialog.values()
            if values.get('start_date') and values.get('due_date') and values['start_date'] > values['due_date']:
                dialog.show_error('截止日期不能早于开始日期'); return
            data = deepcopy(payload(values))
            dialog.set_busy(True)
            def done(value):
                if not alive[0]: return
                dialog.set_busy(False)
                dialog.accept()
                if path == '/workspaces':
                    attempt = getattr(self, '_ws_create_attempt', None)
                    if attempt and attempt[1] == data.get('request_id'):
                        self._ws_create_attempt = None
                if valid():
                    success(value)
                elif self.client is client and self.current == WORKSPACES_NAV_KEY:
                    # Refresh the current page, never the destroyed page's widgets.
                    self._ws_load()
            def failed(message):
                if not alive[0]: return
                dialog.set_busy(False)
                dialog.show_error(str(message))
            self.runner.submit(lambda: client.post(path, data), done, failed)
        dialog.buttons.accepted.connect(save)

    def _ws_open(self):
        workspace = _selected(self.ws_table)
        if not workspace: return
        workspace = deepcopy(workspace)
        snapshot = deepcopy(workspace.get('template_snapshot', {}))
        path = '/workspaces/' + quote(str(workspace['id']), safe='')
        dialog = QDialog(self); dialog.setWindowTitle(workspace.get('name', '专项详情')); dialog.resize(1000, 750)
        box = QVBoxLayout(dialog)
        tabs = QTabWidget(); box.addWidget(tabs)
        overview = QPlainTextEdit(); overview.setReadOnly(True)
        overview.setPlainText(_json({'阶段及交付物/检查清单': workspace.get('phases', snapshot.get('phases', [])),
                                    '模板快照': snapshot, '关联项目': workspace.get('project_id'), '任务': workspace.get('task_ids', [])}))
        tabs.addTab(overview, '阶段与交付物')
        records = _table(['模块', '名称', '记录ID']); tabs.addTab(records, '基础记录')
        entries = _table(['表单', '状态', '版本', '内容', 'ID']); tabs.addTab(entries, '专项表单历史')
        state = {'records': [], 'entries': []}
        def load():
            def got_records(value):
                state['records'] = deepcopy(_rows(value, 'records'))
                _fill(records, state['records'], ['module', lambda r: r.get('data', {}).get('名称', r.get('name', '')), 'id'])
            def got_entries(value):
                state['entries'] = deepcopy(_rows(value, 'entries'))
                _fill(entries, state['entries'], ['form_key', 'status', 'version', lambda r: _json(r.get('data', {})), 'id'])
            self._ws_request('GET', path + '/records', None, got_records, dialog)
            self._ws_request('GET', path + '/entries', None, got_entries, dialog)
        actions = QHBoxLayout(); box.addLayout(actions)
        form_combo = QComboBox()
        for form in snapshot.get('forms', []): form_combo.addItem(form.get('name', form.get('label', form.get('key', ''))), form)
        actions.addWidget(form_combo)
        _button(actions, '填写专项表单', lambda: self._ws_entry(path, form_combo.currentData(), None, load))
        def edit():
            entry = _selected(entries)
            if not entry: return
            form = next((f for f in snapshot.get('forms', []) if f.get('key') == entry.get('form_key')), None)
            self._ws_entry(path, form, entry, load)
        _button(actions, '编辑历史（版本校验）', edit)
        _button(actions, '关联基础资料', lambda: self._ws_link(path, load))
        _button(actions, '刷新详情', load)
        actions = QHBoxLayout(); box.addLayout(actions)
        report = _button(actions, '生成专项报告草稿', lambda: self._ws_request('POST', path + '/report', {},
                         lambda value: self.draft_editor(value.get('draft', value)), dialog, report))
        _button(actions, '专项AI（预览后外发）', lambda: self._ws_ai(path, workspace, dialog))
        load(); dialog.exec()

    def _ws_entry(self, path, form, entry, refresh):
        if not form: return
        fields, choices = entry_fields(form)
        fields.append(('__entry_status', '保存状态 *', ['draft', 'submitted'], 'draft'))
        values = deepcopy((entry or {}).get('data', {})); values['__entry_status'] = (entry or {}).get('status', 'draft')
        dialog = FormDialog(form.get('name', form.get('key', '专项表单')), fields, values, choices, self)
        # FormDialog normally treats every key named "name" as required; here the
        # declarative snapshot is authoritative, including optional name fields.
        dialog._required = {f['key'] for f in form.get('fields', []) if f.get('required')} | {'__entry_status'}
        def payload(data):
            status = data.pop('__entry_status')
            result = {'form_key': form['key'], 'data': data, 'status': status}
            if entry: result.update(id=entry['id'], version=entry['version'])
            return result
        self._ws_bind_save(dialog, payload, path + '/entries', lambda _: refresh())
        dialog.exec()

    def _ws_link(self, path, refresh):
        records = deepcopy(self.records)
        choices = [(f"{r.get('module', '')} · {r.get('data', {}).get('名称', r.get('name', r['id']))} · {r['id']}", r['id']) for r in records]
        dialog = FormDialog('关联已有系统、资产、风险等基础资料', [('record_id', '已有记录 *', 'choice', '')], choices={'record_id': choices}, parent=self)
        self._ws_bind_save(dialog, lambda data: data, path + '/links', lambda _: refresh()); dialog.exec()

    def _ws_ai(self, path, workspace, parent):
        snapshot = deepcopy(workspace.get('template_snapshot', {}))
        prompts = snapshot.get('prompts', [])
        choices = {'provider_id': [(r.get('name', r['id']), r['id']) for r in self.config.get('providers', []) if r.get('enabled', True)],
                   'prompt_name': [(p['name'], p['name']) for p in prompts]}
        dialog = FormDialog('专项AI：选择模板专属提示词', [('provider_id', '外部模型 *', 'choice', ''),
                            ('prompt_name', '模板提示词 *', 'choice', '')], choices=choices, parent=parent)
        if not dialog.exec(): return
        data = dialog.values()
        # The server renders the exact context, including all expanded references.
        client = self.client; valid = self._ws_guard(); alive = [True]
        parent.finished.connect(lambda *_: alive.__setitem__(0, False))
        def loaded(values):
            if not valid() or not alive[0]: return
            if not isinstance(values,dict) or not values.get('context_hash'):
                self.error('专项预览响应无效，请刷新或升级后台服务。');return
            preview = QDialog(parent); preview.setWindowTitle('即将外发的专项资料预览'); preview.resize(850, 650)
            box = QVBoxLayout(preview); text = QPlainTextEdit(); text.setReadOnly(True)
            text.setPlainText(_json({'外发完整上下文': values, 'provider_id': data['provider_id']})); box.addWidget(text)
            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel); box.addWidget(buttons)
            buttons.button(QDialogButtonBox.Ok).setText('确认外发（可能产生费用）')
            buttons.accepted.connect(preview.accept); buttons.rejected.connect(preview.reject)
            if not preview.exec(): return
            # Always require a second authorization: free-text forms can contain sensitive data.
            if QMessageBox.question(parent, '敏感资料二次确认', '专项表单及关联资料可能包含人员、权限、商业秘密等敏感内容。确认有权将刚预览的全部资料交给所选外部模型？',
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes: return
            if not valid() or not alive[0]: return
            self._ws_request('POST', path + '/ai', dict(data, confirmed=True, sensitive_confirmed=True,context_hash=values.get('context_hash','')),
                             lambda value: self.draft_editor(value.get('draft', value)), parent)
        def failed(message):
            if valid() and alive[0]: self.error(str(message))
        self.runner.submit(lambda: client.get(path + '/ai-preview?prompt_name=' + quote(data['prompt_name'], safe='')), loaded, failed)

    def _ws_template_edit(self, checked=False, copy=False, new=False):
        row = _selected(self.ws_template_table) if not new else None
        if not row and not new: return
        config = deepcopy(row.get('config', {})) if row else {'schema_version': 1, 'name': '', 'description': '', 'phases': [{'key': 'prepare', 'name': '准备', 'deliverables': [], 'checklist': []}], 'work_packages': [], 'forms': [], 'prompts': [], 'report_sections': ['阶段进展', '风险与下一步']}
        if copy: config['name'] = config.get('name', '') + '（副本）'
        dialog = TemplateEditor(config, self)
        if not dialog.exec(): return
        payload = {'data': dialog.values()}
        if row and not copy:
            payload['id'] = row['id']
            payload['version'] = row['version']
        self._ws_request('POST', '/workspace-templates', payload, lambda _: self._ws_load())

    def _ws_import(self):
        filename, _ = QFileDialog.getOpenFileName(self, '导入模板JSON', '', 'JSON (*.json)')
        if not filename: return
        try:
            value = json.loads(Path(filename).read_text(encoding='utf-8-sig'))
            config = value.get('config', value) if isinstance(value, dict) else None
            if not isinstance(config, dict) or not isinstance(config.get('name'), str): raise ValueError('需要包含name的模板配置对象')
            dialog = TemplateEditor(config, self); dialog.setWindowTitle('导入预览：核对并保存为新模板（不执行代码）')
            if dialog.exec(): self._ws_request('POST', '/workspace-templates', {'data': dialog.values()}, lambda _: self._ws_load())
        except (OSError, ValueError, TypeError) as exc:
            self.error(str(exc))

    def _ws_export(self):
        row = _selected(self.ws_template_table)
        if not row: return
        filename, _ = QFileDialog.getSaveFileName(self, '导出模板配置JSON', '', 'JSON (*.json)')
        if not filename: return
        def save(value):
            try: Path(filename).write_text(_json(value), encoding='utf-8')
            except OSError as exc: self.error(str(exc))
        self._ws_request('GET', '/workspace-templates/' + quote(str(row['id']), safe='') + '/export', None, save)
