"""Independent, schema-driven editors. No network requests or gui imports."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QWidget, QScrollArea, QTabWidget,
    QLabel, QLineEdit, QPlainTextEdit, QComboBox, QCheckBox, QSpinBox,
    QDoubleSpinBox, QDialogButtonBox, QMessageBox, QCalendarWidget,
    QToolButton, QHBoxLayout, QPushButton,
)
from .schema import MODULES, STATUS
from .store import REFERENCES, NUMBERS
from .ai import DEFAULT_PROVIDERS

RELATIONS = REFERENCES
LONG_WORDS = ('内容', '证据', '说明', '评价', '反馈', '目标', '标准', '备注', '过程',
              '措施', '计划', '复盘', '职责', '安排', '原因', '下一步', '风险', '对比', '建议', '步骤')
PEOPLE_FIELDS = {'负责人', '业务负责人', '项目负责人', '技术负责人', '责任人', '主管',
                 '验收人', '复核人', '提供人', '批准人', '执行人', '提出人', '决策人', 'owner'}
REVIEW_STATUS = ['目标设定', '待评价', '待反馈', '待复核', '已定稿']
EMAIL_FIELDS = {'host', 'port', 'security', 'username_env', 'password_env', 'from_addr', 'to_addrs'}
BOT_FIELDS = {'webhook_env', 'secret_env'}


def ask(parent, text):
    return QMessageBox.question(parent, '确认操作', text, QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) == QMessageBox.Yes


class NullableDateEdit(QLineEdit):
    """A nullable ISO date, preserving even invalid typed input for inline validation."""
    def __init__(self, value='', parent=None):
        super().__init__(str(value or ''), parent)
        self.setPlaceholderText('YYYY-MM-DD（可留空）')
        self.setClearButtonEnabled(True)
        self.calendar_button = QToolButton(self)
        self.calendar_button.setText('▦')
        self.calendar_button.setToolTip('选择日期')
        self.calendar_button.setAccessibleName('打开日期日历')
        self.setTextMargins(0, 0, 52, 0)
        self.popup = QDialog(self, Qt.Popup)
        box = QVBoxLayout(self.popup)
        self.calendar = QCalendarWidget(self.popup)
        box.addWidget(self.calendar)
        clear = QPushButton('清空日期')
        box.addWidget(clear)
        clear.clicked.connect(self._clear_date)
        self.calendar.clicked.connect(self._choose_date)
        self.calendar_button.clicked.connect(self.show_calendar)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.calendar_button.setGeometry(max(0, self.width() - 51), 1, 25, self.height() - 2)

    def calendarPopup(self):
        return True

    def show_calendar(self):
        parsed = QDate.fromString(self.text(), 'yyyy-MM-dd')
        self.calendar.setSelectedDate(parsed if parsed.isValid() else QDate.currentDate())
        self.popup.move(self.mapToGlobal(self.rect().bottomLeft()))
        self.popup.show()

    def _choose_date(self, selected):
        self.setText(selected.toString('yyyy-MM-dd'))
        self.popup.hide()

    def _clear_date(self):
        self.clear()
        self.popup.hide()


class FormDialog(QDialog):
    """Compatible widgets/values API, with optional validation and busy-state hooks."""
    def __init__(self, title, fields, values=None, choices=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(740, 660)
        self.widgets = {}
        self.original = dict(values or {})
        self.choices = choices or {}
        self.fields = list(fields)
        self.validation_error = ''
        self.busy = False
        self._rows = {}
        self._hidden_keys = set()
        self._locked_keys = set()
        self._labels = {key: label.replace(' *', '') for key, label, _, _ in self.fields}
        self._required = {key for key, label, _, _ in self.fields if '*' in label or key == 'name'}
        self.config_kind = ('providers' if {'provider', 'api_key_env'} <= self._labels.keys()
                            else 'channels' if {'type', 'webhook_env', 'host'} <= self._labels.keys() else None)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        heading = QLabel(title)
        heading.setStyleSheet('font-size:20px;font-weight:600;')
        layout.addWidget(heading)
        layout.addWidget(QLabel('带 * 的项目为必填；其他信息可稍后补充。'))
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.forms = []
        for caption in ('基础信息', '执行与验收' if getattr(self, 'module', None) == 'tasks' else '详细信息'):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.NoFrame)
            inside = QWidget()
            form = QFormLayout(inside)
            form.setContentsMargins(16, 16, 16, 16)
            form.setVerticalSpacing(14)
            form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            form.setRowWrapPolicy(QFormLayout.WrapLongRows)
            scroll.setWidget(inside)
            self.tabs.addTab(scroll, caption)
            self.forms.append(form)
        self.form = self.forms[0]
        for key, label, kind, default in self.fields:
            value = self.original.get(key, default)
            if kind == 'bool':
                widget = QCheckBox('启用')
                widget.setChecked(bool(value))
            elif kind in ('int', 'float'):
                widget = QSpinBox() if kind == 'int' else QDoubleSpinBox()
                widget.setRange(0, 10000000)
                widget.setValue(float(value or 0) if kind == 'float' else int(value or 0))
            elif isinstance(kind, list) or key in self.choices:
                widget = QComboBox()
                if key in self.choices:
                    widget.addItem('（无）', '')
                    for display, identifier in self.choices[key]:
                        widget.addItem(display, identifier)
                    if key in PEOPLE_FIELDS:
                        widget.setEditable(True)
                        widget.setInsertPolicy(QComboBox.NoInsert)
                        widget.completer().setCaseSensitivity(Qt.CaseInsensitive)
                else:
                    for option in kind:
                        widget.addItem(option, option)
                index = widget.findData(value)
                if index < 0 and value:
                    widget.addItem(str(value), value)
                    index = widget.count() - 1
                widget.setCurrentIndex(max(0, index))
            elif kind == 'long':
                widget = QPlainTextEdit(str(value or ''))
                widget.setMinimumHeight(82)
                widget.setMaximumHeight(128)
            elif kind == 'date' or key.endswith('日期'):
                widget = NullableDateEdit(value)
            else:
                if key == 'to_addrs' and isinstance(value, list):
                    value = ', '.join(value)
                widget = QLineEdit(str(value or ''))
                if kind == 'password':
                    widget.setEchoMode(QLineEdit.Password)
            widget.setAccessibleName(self._labels[key])
            widget.setObjectName('field_' + key)
            self.widgets[key] = (widget, kind)
            detail = kind == 'long' or key in {'验收人', '评分', '复核人', 'temperature', 'max_tokens', 'timeout'}
            section = 1 if detail and key not in self._required else 0
            field_label = QLabel(self._labels[key] + (' *' if key in self._required else ''))
            if key in self._required:
                field_label.setStyleSheet('font-weight:600;color:#1763a6;')
            self.forms[section].addRow(field_label, widget)
            self._rows[key] = (self.forms[section], widget, section)
        self.provider_hint = QLabel()
        self.provider_hint.setWordWrap(True)
        self.provider_defaults_button = QPushButton('使用此平台默认值（覆盖模型、地址和密钥引用）')
        self.provider_defaults_button.clicked.connect(self.apply_provider_defaults)
        layout.addWidget(self.provider_hint)
        layout.addWidget(self.provider_defaults_button)
        self.provider_hint.setVisible(self.config_kind == 'providers')
        self.provider_defaults_button.setVisible(self.config_kind == 'providers')
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
        if self.config_kind == 'channels':
            self.widgets['type'][0].currentIndexChanged.connect(self._update_channel)
            self._update_channel()
        if self.config_kind == 'providers':
            self.widgets['provider'][0].currentIndexChanged.connect(self._update_provider)
            self._update_provider()
        self.baseline = self.values()
        self._input_baseline = self._all_values()

    def _all_values(self):
        result = dict(self.original)
        for key, (widget, kind) in self.widgets.items():
            if isinstance(widget, QCheckBox):
                value = widget.isChecked()
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                value = widget.value()
            elif isinstance(widget, QComboBox):
                value = widget.currentText().strip() if widget.isEditable() else widget.currentData()
                if widget.isEditable() and widget.currentIndex() == 0 and widget.currentText() == '（无）':
                    value = ''
            elif isinstance(widget, QPlainTextEdit):
                value = widget.toPlainText()
            else:
                value = widget.text().strip()
            result[key] = value
        return result

    def values(self):
        result = self._all_values()
        for key in self._hidden_keys:
            result.pop(key, None)
        return result

    def _update_channel(self, *_):
        kind = self.widgets['type'][0].currentData()
        inactive = BOT_FIELDS if kind == 'email' else EMAIL_FIELDS | ({'secret_env'} if kind == 'wecom' else set())
        self._hidden_keys = set(inactive)
        for key in EMAIL_FIELDS | BOT_FIELDS:
            if key in self._rows:
                form, widget, _ = self._rows[key]
                form.setRowVisible(widget, key not in inactive)

    def _update_provider(self, *_):
        defaults = DEFAULT_PROVIDERS.get(self.widgets['provider'][0].currentData(), {})
        self.provider_hint.setText('此平台建议：' + ' · '.join(str(defaults.get(k, '')) for k in ('model', 'base_url', 'api_key_env')) + '\n切换平台不会覆盖已有输入；需要时点击下方按钮明确应用。')
        for key in ('model', 'base_url', 'api_key_env'):
            if key in self.widgets:
                self.widgets[key][0].setPlaceholderText(defaults.get(key, ''))

    def apply_provider_defaults(self):
        defaults = DEFAULT_PROVIDERS.get(self.widgets['provider'][0].currentData(), {})
        for key in ('model', 'base_url', 'api_key_env'):
            if key in self.widgets and key in defaults:
                self.widgets[key][0].setText(defaults[key])

    def show_error(self, message, key=None):
        self.validation_error = str(message or '')
        self.error_label.setText(self.validation_error)
        self.error_label.setVisible(bool(message))
        if key in self._rows and key not in self._hidden_keys:
            _, widget, section = self._rows[key]
            self.tabs.setCurrentIndex(section)
            widget.setFocus()

    def _fail(self, key, message):
        self.show_error(self._labels.get(key, key) + '：' + message, key)
        return False

    def validate(self):
        self.show_error('')
        data = self.values()
        for key in self._required - self._hidden_keys:
            if not str(data.get(key, '')).strip():
                return self._fail(key, '不能为空')
        for key, value in data.items():
            if key not in self.widgets or not value:
                continue
            if key.endswith('日期') or self.widgets[key][1] == 'date':
                try:
                    if date.fromisoformat(value).isoformat() != value:
                        raise ValueError()
                except (ValueError, TypeError):
                    return self._fail(key, '请输入有效日期 YYYY-MM-DD，或留空')
            if key.endswith('_env') and not re.fullmatch(r'[A-Z_][A-Z0-9_]{0,127}', str(value)):
                return self._fail(key, '请填环境变量名称，不要粘贴真实密钥或URL')
        if data.get('开始日期') and data.get('截止日期') and data['开始日期'] > data['截止日期']:
            return self._fail('截止日期', '不能早于开始日期')
        if self.config_kind:
            from .validation import validate_config
            candidate = dict(data)
            if self.config_kind == 'channels' and candidate.get('type') == 'email':
                candidate['to_addrs'] = [x.strip() for x in candidate.get('to_addrs', '').split(',') if x.strip()]
            try:
                validate_config(self.config_kind, candidate)
            except ValueError as exc:
                self.show_error(str(exc))
                return False
        return True

    def _accept_validated(self):
        if not self.busy and self.validate():
            self.accept()

    def set_busy(self, busy=True):
        self.busy = bool(busy)
        self.tabs.setEnabled(not busy)
        self.buttons.setEnabled(not busy)
        self.provider_defaults_button.setEnabled(not busy)
        self.buttons.button(QDialogButtonBox.Save).setText('保存中…' if busy else '保存')

    def is_dirty(self):
        return self._all_values() != self._input_baseline

    def reject(self):
        if self.busy or not self.buttons.isEnabled():
            self.show_error('正在保存，请等待完成；当前输入已保留。')
            return
        if self.is_dirty() and not ask(self, '放弃尚未保存的修改？'):
            return
        super().reject()


class RecordDialog(FormDialog):
    def __init__(self, module, record=None, records=(), parent=None):
        self.module = module
        self.record = record or {}
        self.records = list(records)
        self._final_review = module == 'reviews' and self.record.get('data', {}).get('状态') == '已定稿'
        title, names = MODULES[module]
        fields = []
        choices = {}
        people = [r for r in self.records if r.get('module') == 'people' and not r.get('archived')]
        for index, name in enumerate(names):
            kind = ((REVIEW_STATUS if module == 'reviews' else STATUS) if name == '状态' else
                    ['', '高', '中', '低'] if name == '优先级' else
                    'date' if name.endswith('日期') else
                    'long' if any(word in name for word in LONG_WORDS) else 'text')
            fields.append((name, name + (' *' if index == 0 else ''), kind, ''))
            if name in REFERENCES:
                options = []
                for row in self.records:
                    if row.get('archived') or row.get('id') == self.record.get('id'):
                        continue
                    target = REFERENCES[name]
                    if target and row.get('module') != target:
                        continue
                    row_module = row.get('module')
                    if row_module not in MODULES:
                        continue
                    caption, keys = MODULES[row_module]
                    label = str(row.get('data', {}).get(keys[0]) or '未命名')
                    options.append((f'{label} · {caption}', row['id']))
                choices[name] = options
            elif name in PEOPLE_FIELDS and people:
                choices[name] = [(r['data'].get('姓名', ''), r['data'].get('姓名', '')) for r in people if r['data'].get('姓名')]
        super().__init__(title, fields, self.record.get('data'), choices, parent)
        if self._final_review:
            for name, (widget, _) in self.widgets.items():
                widget.setEnabled(name == '状态')
            combo = self.widgets['状态'][0]
            combo.clear()
            for state in ('已定稿', '待复核'):
                combo.addItem(state, state)
            note = QLabel('已定稿考核仅允许改为「待复核」。保存后再编辑内容。')
            note.setWordWrap(True)
            self.form.addRow(note)
        self.baseline = self.values()
        self._input_baseline = self._all_values()

    def values(self):
        data = super().values()
        if self._final_review:
            return dict(self.original, 状态=data['状态'])
        return data

    def validate(self):
        if self._final_review:
            self.show_error('')
            if self.values().get('状态') != '待复核':
                return self._fail('状态', '已定稿考核须先改为待复核，再保存')
            return True
        if not super().validate():
            return False
        data = self.values()
        for key, value in data.items():
            if len(str(value)) > 50000:
                return self._fail(key, '不能超过50000字符')
            if not value:
                continue
            if key in NUMBERS:
                try:
                    if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)', value) or Decimal(value) < 0:
                        raise InvalidOperation()
                except (InvalidOperation, TypeError):
                    return self._fail(key, '请输入非负十进制数值')
            if key in {'受理时间', '恢复时间'}:
                try:
                    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?', value):
                        raise ValueError()
                    datetime.fromisoformat(value)
                except ValueError:
                    return self._fail(key, '请输入有效ISO日期时间，如 2026-03-01 09:00')
        if self.module == 'tasks' and data.get('状态') == '已完成':
            for key in ('完成证据', '验收人'):
                if not data.get(key, '').strip():
                    return self._fail(key, '完成任务时必填')
        if self.module == 'reviews':
            if data.get('评分') and Decimal(data['评分']) > 100:
                return self._fail('评分', '应在0—100之间')
            if data.get('状态') not in REVIEW_STATUS:
                return self._fail('状态', '请选择有效考核状态')
            if data.get('状态') == '已定稿':
                for key in ('人员ID', '考核周期', '成果证据', '主管评价', '员工反馈', '复核人'):
                    if not data.get(key, '').strip():
                        return self._fail(key, '考核定稿时必填')
        return True
