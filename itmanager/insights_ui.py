"""提醒中心与台账数据体检：纯页面实现，由 MainWindow 接线（导航键 reminders / health）。

宿主 MainWindow 需要提供：page_layout、records、client、runner.submit(work, done, failed)、
generation、current、keys/nav、statusBar()、refresh()、edit_record(module, record)。

本模块不修改任何记录：提醒只做程序内提示，体检只读扫描；建立整改任务必须人工确认后才会发起。
reminders / health 服务缺失或报错时页面降级显示原因，而不是崩溃。
"""
from __future__ import annotations

from copy import deepcopy
import csv
import io
import json
import re
from datetime import date, timedelta
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

try:  # 依赖由父代理的其他子任务实现；缺失时页面降级，而不是导入失败。
    from . import reminders as reminders_service
except Exception:  # pragma: no cover - 仅在依赖文件缺失时触发
    reminders_service = None

try:  # 处理状态（已处理 / 稍后提醒 / 忽略、已整改 / 误报忽略）
    from . import reminder_state as reminder_state_service
except Exception:  # pragma: no cover
    reminder_state_service = None

try:
    from . import health_state as health_state_service
except Exception:  # pragma: no cover
    health_state_service = None

try:
    from . import health as health_service
except Exception:  # pragma: no cover - 仅在依赖文件缺失时触发
    health_service = None

try:
    from .schema import MODULES
except Exception:  # pragma: no cover
    MODULES = {}

from .forms import FormDialog

try:
    from .management_ui import ActionCreationDialog
except Exception:  # pragma: no cover - 仅用于极端降级
    ActionCreationDialog = None

REMINDERS_NAV_KEY = 'reminders'
HEALTH_NAV_KEY = 'health'
REMINDERS_NAV_LABEL = '提醒中心'
HEALTH_NAV_LABEL = '台账数据体检'

REMINDER_COLUMNS = ['等级', '类型', '模块', '事项', '日期', '剩余天数', '建议动作']
HEALTH_COLUMNS = ['等级', '模块', '事项', '字段', '问题说明', '建议']
LEVELS = ('高', '中', '低')
LEVEL_ORDER = {'高': 0, '中': 1, '低': 2}
LEVEL_ALIASES = {'高': '高', '高风险': '高', '高危': '高', '严重': '高', '重大': '高', '极高': '高',
                 'high': '高', 'critical': '高',
                 '中': '中', '中风险': '中', 'medium': '中', 'moderate': '中',
                 '低': '低', '低风险': '低', '提示': '低', 'info': '低', 'low': '低'}
SUMMARY_KEYS = {'total': 'total', '高': 'high', '中': 'medium', '低': 'low'}
# reminders 规则对象的 10 个权威键（validate_rule 多一个键就报“包含未知字段”）。
RULE_KEYS = ('key', 'name', 'type', 'modules', 'date_fields', 'warn_days',
             'stale_days', 'severity', 'enabled', 'note')
RULE_MULTI_KEYS = ('modules', 'date_fields')
RULE_TYPES = (('expiry', '到期提醒（expiry）'), ('overdue', '逾期提醒（overdue）'),
              ('stale', '长期未更新（stale）'))
RULE_TYPES_LABELS = dict(RULE_TYPES)
RULE_TYPES_DISPLAY = [label for _, label in RULE_TYPES]  # FormDialog 的普通列表选项就是显示文本
RULE_TYPE_BY_LABEL = {label: key for key, label in RULE_TYPES}
STALE_MODULE_KEY = '__stale_all__'
DEFAULT_STALE_DAYS = 180
DEFAULT_WARN_DAYS = 30
KIND_ALIASES = {'expiry': '到期提醒', 'overdue': '逾期提醒', 'stale': '长期未更新'}
# health 发现项的键；导出时只使用这些字段，避免把其他内容（含潜在凭据）带出去。
FINDING_KEYS = ('code', 'severity', 'module', 'record_id', 'title', 'field', 'detail', 'suggestion')
DEGRADED_RULES_NOTICE = '规则读取失败，当前为内置默认规则，修改将失败。'
UNAVAILABLE_REMINDERS_NOTICE = '提醒服务不可用（未安装 reminders 模块）：当前只展示内置默认规则，保存与删除均不会生效。'
UNAVAILABLE_HEALTH_NOTICE = '体检服务不可用（未安装 health 模块）：当前只显示提示，不会执行任何扫描。'
HEALTH_DISCLAIMER = '未发现明显问题不等于合规结论；无证据不代表造假，只提示补齐材料。'
EXPORT_FILTERS = 'CSV (*.csv);;Markdown (*.md)'
_TABLE_QSS = """
QFrame[card="true"] { background:white; border:1px solid #dde4ee; border-radius:9px; }
QLabel[stat="true"] { font-size:15px; font-weight:600; }
QLabel[level="high"] { color:#b42318; font-weight:600; }
QLabel[level="mid"] { color:#b54708; font-weight:600; }
QLabel[level="low"] { color:#175cd3; font-weight:600; }
"""


# --------------------------------------------------------------------------------------
# 依赖可用性
# --------------------------------------------------------------------------------------
def reminders_api():
    module = reminders_service
    return module if module is not None else None


def reminders_available():
    module = reminders_api()
    return all(callable(getattr(module, name, None)) for name in ('compute_items', 'validate_rule'))


def health_api():
    return health_service if health_service is not None else None


def reminder_state():
    """处理状态服务；缺失时抛 ValueError，由调用方转成页面提示。"""
    module = reminder_state_service
    if module is None:
        raise ValueError('处理状态服务不可用，请升级后台服务后重试。')
    return module


def health_state():
    module = health_state_service
    if module is None:
        raise ValueError('处理状态服务不可用，请升级后台服务后重试。')
    return module


def _checked(owner, attribute):
    """Read a checkbox on the current page; stale Qt wrappers count as unchecked."""
    widget = getattr(owner, attribute, None)
    try:
        return bool(widget is not None and widget.isChecked())
    except RuntimeError:  # the page was rebuilt and the C++ object is gone
        return False


def _alive(widget):
    """True only for a live Qt widget; page attributes can outlive their widgets."""
    if widget is None:
        return False
    try:
        widget.objectName()
    except RuntimeError:
        return False
    return True


def _apply_reminder_states(window, items):
    """Filter reminder items through stored handled/snoozed/dismissed states."""
    module = reminder_state_service
    if module is None or not items:
        return items
    states = getattr(window, '_reminder_states', None) or []
    include = _checked(window, 'reminders_show_handled')
    try:
        return module.apply_states(items, states, date.today(), include_handled=include)
    except Exception:
        return items


def _apply_health_states(window, findings):
    module = health_state_service
    if module is None or not findings:
        return findings
    states = getattr(window, '_health_states', None) or []
    include = _checked(window, 'health_show_handled')
    try:
        return module.apply_states(findings, states, include_resolved=include)
    except Exception:
        return findings


def health_available():
    module = health_api()
    return bool(module is not None and callable(getattr(module, 'scan', None)))


def _call(module, name, *args, **kwargs):
    """在本地扫描线程外调用依赖函数：异常一律转为 ValueError 交给页面提示。"""
    function = getattr(module, name, None)
    if not callable(function):
        raise ValueError('缺少依赖函数：' + name)
    return function(*args, **kwargs)


def _rows_of(value, key=None):
    if isinstance(value, dict) and key:
        value = value.get(key, [])
    if not isinstance(value, list):
        raise ValueError('服务端返回的列表格式无效。')
    return value


# --------------------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------------------
def _button(text, callback, layout, primary=False):
    button = QPushButton(text)
    button.setProperty('primary', primary)
    button.clicked.connect(callback)
    layout.addWidget(button)
    return button


def _table(headers, checkable=False, columns=None):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.ExtendedSelection if checkable else QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setWordWrap(False)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(38)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.Stretch)
    header.setMinimumSectionSize(60)
    header.setMaximumSectionSize(420)
    return table


def _cell(text, payload=None, checkable=False):
    item = QTableWidgetItem(str(text if text is not None else ''))
    if checkable:
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Unchecked)
    if payload is not None:
        item.setData(Qt.UserRole, payload)
    return item


def _selected(table):
    item = table.item(table.currentRow(), 0)
    return item.data(Qt.UserRole) if item else None


def _as_date(value):
    """把 YYYY-MM-DD / YYYY-MM-DDTHH:MM / date 归一化为 date，无法解析返回 None。"""
    if isinstance(value, date):
        return value
    text = str(value or '').strip()
    if len(text) >= 10:
        text = text[:10]
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed


def iso_date(value):
    parsed = _as_date(value)
    return parsed.isoformat() if parsed else ''


def _level(row, *keys):
    for key in keys:
        raw = row.get(key) if isinstance(row, dict) else None
        if raw is None or str(raw).strip() == '':
            continue
        text = str(raw).strip()
        return LEVEL_ALIASES.get(text, LEVEL_ALIASES.get(text.lower(), '中'))
    return '中'


def _text(row, *keys, default=''):
    for key in keys:
        if isinstance(row, dict):
            raw = row.get(key)
            if raw is not None and str(raw).strip() != '':
                return str(raw).strip()
    return default


def _module_title(key):
    if not key:
        return ''
    entry = MODULES.get(key)
    return entry[0] if entry else str(key)


def _stat_bar(layout, stats):
    """父代理 QSS 卡片风格统计条。stats: [(标题, 数量, 说明)]"""
    bar = QHBoxLayout()
    for title, count, hint in stats:
        card = QFrame()
        card.setProperty('card', True)
        box = QVBoxLayout(card)
        value = QLabel(f'{title} {count}')
        value.setProperty('stat', True)
        box.addWidget(value)
        if hint:
            note = QLabel(hint)
            note.setWordWrap(True)
            box.addWidget(note)
        bar.addWidget(card, 1)
    layout.addLayout(bar)
    return bar


def _message_label(text=''):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.PlainText)
    return label


def _reset_layout(widget):
    """清空并移除旧布局，保证重复渲染不会叠加统计卡片。"""
    old = widget.layout()
    if old is not None:
        while old.count():
            item = old.takeAt(0)
            child = item.widget()
            if child is not None:
                child.setParent(None)
                child.deleteLater()
        QWidget().setLayout(old)


def _stat_card(title, value, hint=''):
    card = QFrame()
    card.setProperty('card', True)
    inner = QVBoxLayout(card)
    label = QLabel(f'{title} {value}')
    label.setProperty('stat', True)
    inner.addWidget(label)
    if hint:
        inner.addWidget(QLabel(hint))
    return card


def _hint(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet('background:#eaf2fd;border-radius:8px;padding:10px;line-height:1.5')
    return label


# --------------------------------------------------------------------------------------
# 提醒：规则模型、解析、展示（严格对齐 reminders 的权威键）
# --------------------------------------------------------------------------------------
def reminder_module_choices():
    return [(title, name) for name, (title, _) in MODULES.items()]


def rule_fields():
    """规则表单字段：字段名就是服务端 validate_rule 接受的 10 个键。"""
    return [
        ('key', '规则 key *（字母开头，可含数字/下划线/连字符）', 'text', ''),
        ('name', '规则名称 *', 'text', ''),
        ('type', '提醒类型', RULE_TYPES_DISPLAY, '到期提醒（expiry）'),
        ('modules', '适用模块 *（可多选）', 'choice', 'contracts'),
        ('date_fields', '日期字段 *（可多选，必须是模块内真实字段）', 'choice', '到期日期'),
        ('warn_days', '提前提醒天数（0—365）', 'int', DEFAULT_WARN_DAYS),
        ('stale_days', '未更新天数（1—3650）', 'int', DEFAULT_STALE_DAYS),
        ('severity', '等级', list(LEVELS), '中'),
        ('enabled', '启用', 'bool', True),
        ('note', '说明（仅本页展示，不会外发）', 'long', ''),
    ]


def rule_time_fields(module_keys):
    """所选模块下真实存在的日期类字段（服务端会按模块 schema 校验）。"""
    fields = []
    for key in module_keys or []:
        for field in MODULES.get(key, ('', []))[1]:
            if '日期' in field and field not in fields:
                fields.append(field)
    return fields


def rule_values(rule):
    """把服务端规则（含外层 id/version 与 data 包装）归一化为可编辑表单值。"""
    source = rule.get('data') if isinstance(rule, dict) and isinstance(rule.get('data'), dict) else rule
    source = source if isinstance(source, dict) else {}
    values = {key: source[key] for key in RULE_KEYS if key in source}
    for optional in ('id', 'version'):
        if isinstance(rule, dict) and rule.get(optional) is not None:
            values[optional] = rule[optional]
    values.setdefault('enabled', True)
    modules = values.get('modules')
    values['modules'] = [str(item) for item in modules] if isinstance(modules, list) and modules else ['contracts']
    fields = values.get('date_fields')
    values['date_fields'] = [str(item) for item in fields] if isinstance(fields, list) else rule_time_fields(values['modules'])[:1]
    try:
        values['warn_days'] = int(values.get('warn_days', DEFAULT_WARN_DAYS))
    except (TypeError, ValueError):
        values['warn_days'] = DEFAULT_WARN_DAYS
    try:
        values['stale_days'] = int(values.get('stale_days', DEFAULT_STALE_DAYS))
    except (TypeError, ValueError):
        values['stale_days'] = DEFAULT_STALE_DAYS
    values['severity'] = _level(values, 'severity')
    values['type'] = values.get('type') if values.get('type') in dict(RULE_TYPES) else 'expiry'
    return values


def rule_payload(values):
    """只保留 10 个规范键；绝不多传字段，否则服务端会报“未知字段”。"""
    values = values if isinstance(values, dict) else {}
    payload = {key: values[key] for key in RULE_KEYS if key in values}
    modules = payload.get('modules')
    if isinstance(modules, str):
        modules = [item.strip() for item in modules.split(',') if item.strip()]
    modules = [str(item) for item in modules] if isinstance(modules, list) else []
    if modules and modules[0] == STALE_MODULE_KEY:
        modules = list(MODULES)
    payload['modules'] = [item for item in modules if item in MODULES]
    fields = payload.get('date_fields')
    if isinstance(fields, str):
        fields = [item.strip() for item in fields.split(',') if item.strip()]
    payload['date_fields'] = [str(item) for item in fields] if isinstance(fields, list) else []
    for key, default in (('warn_days', DEFAULT_WARN_DAYS), ('stale_days', DEFAULT_STALE_DAYS)):
        try:
            payload[key] = int(payload.get(key, default))
        except (TypeError, ValueError):
            payload[key] = default
    payload['name'] = str(payload.get('name', '')).strip()
    payload['key'] = str(payload.get('key', '')).strip()
    payload['severity'] = _level(payload, 'severity')
    payload['type'] = payload.get('type') if payload.get('type') in dict(RULE_TYPES) else 'expiry'
    payload['enabled'] = bool(payload.get('enabled', True))
    payload['note'] = str(payload.get('note') or '')
    return {key: payload[key] for key in RULE_KEYS}


def validate_rule_local(data):
    """reminders 不可用时的本地校验：与权威规则同构（key/名称/模块/日期字段/天数/等级）。"""
    if not isinstance(data, dict):
        raise ValueError('提醒规则结构无效')
    payload = rule_payload(data)
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', payload['key'] or ''):
        raise ValueError('提醒规则key无效')
    if not payload['name'] or len(payload['name']) > 200:
        raise ValueError('提醒规则名称无效')
    if payload['type'] not in dict(RULE_TYPES):
        raise ValueError('提醒规则类型无效')
    if not payload['modules']:
        raise ValueError('提醒规则的业务模块无效')
    allowed = {field for module in payload['modules'] for field in MODULES[module][1]}
    if any(field not in allowed for field in payload['date_fields']):
        raise ValueError('提醒规则日期字段不符合模块定义')
    if payload['type'] == 'expiry' and not payload['date_fields']:
        raise ValueError('提醒规则日期字段不符合模块定义')
    if not 0 <= payload['warn_days'] <= 365:
        raise ValueError('提醒提前天数须为0—365')
    if not 1 <= payload['stale_days'] <= 3650:
        raise ValueError('提醒未更新天数须为1—3650')
    if payload['severity'] not in LEVELS:
        raise ValueError('提醒等级无效')
    return None


def validate_rule(data):
    """优先使用 reminders.validate_rule（权威）；不可用时退回本地同构校验。"""
    module = reminders_api()
    validator = getattr(module, 'validate_rule', None) if module is not None else None
    if callable(validator):
        return validator(data)
    return validate_rule_local(data)


def default_rules():
    """服务不可用或读取失败时的内置默认规则（能通过 validate_rule，只用于展示）。"""
    module = reminders_api()
    factory = getattr(module, 'default_rules', None) if module is not None else None
    if callable(factory):
        try:
            rules = factory()
            if isinstance(rules, list) and rules:
                return rules
        except Exception:
            pass
    return [
        {'key': 'contract_expiry', 'name': '合同到期提醒', 'type': 'expiry',
         'modules': ['contracts'], 'date_fields': ['到期日期'], 'warn_days': 60,
         'stale_days': 365, 'severity': '高', 'enabled': True,
         'note': '合同到期前60天提醒；同时人工核对通知截止日期、SLA与退出安排。'},
        {'key': 'task_overdue', 'name': '任务逾期提醒', 'type': 'overdue',
         'modules': ['tasks'], 'date_fields': ['截止日期'], 'warn_days': 0,
         'stale_days': 180, 'severity': '高', 'enabled': True,
         'note': '截止日期已过且任务未结束；已完成、已取消、已关闭任务不提醒。'},
        {'key': 'stale_open_record', 'name': '长期未更新提醒', 'type': 'stale',
         'modules': list(MODULES), 'date_fields': [], 'warn_days': 0,
         'stale_days': 180, 'severity': '低', 'enabled': True,
         'note': '未结束记录超过180天未更新时提醒，请更新进展或归档。'},
    ]


def reminder_row(item):
    """把 compute_items 的一项映射为表格展示行（严格使用其权威键）。"""
    item = item if isinstance(item, dict) else {}
    days = item.get('days_left')
    if not isinstance(days, int):
        parsed, due = None, _as_date(item.get('date'))
        if due:
            parsed = (due - date.today()).days
        days = parsed
    overdue = item.get('overdue_days')
    if days is not None and days < 0 and isinstance(overdue, int) and overdue > 0:
        days_text = f'已逾期{overdue}天'
    elif days is None:
        days_text = '无日期'
    elif days < 0:
        days_text = f'已逾期{abs(days)}天'
    elif days == 0:
        days_text = '今天到期'
    else:
        days_text = f'剩余{days}天'
    row = {
        'level': _level(item, 'severity', 'level'),
        'kind': _text(item, 'rule_name', 'rule_key', 'kind', 'type'),
        'module': _text(item, 'module'),
        'title': _text(item, 'title', 'name', default='未命名事项'),
        'due_date': iso_date(item.get('date')) or _text(item, 'date'),
        'days': days,
        'days_text': days_text,
        'advice': _text(item, 'message', 'advice', 'suggestion'),
        'record_id': _text(item, 'record_id', 'id'),
        'sort': item.get('sort') if isinstance(item.get('sort'), list) else None,
        'raw': item,
    }
    return row


def sort_reminders(rows):
    """稳定为 严重度→剩余天数→模块→标题；compute_items 已排好序时保持其顺序。"""
    if rows and all(isinstance(row.get('sort'), list) for row in rows):
        return sorted(rows, key=lambda row: list(row['sort']))
    return sorted(rows, key=lambda row: (LEVEL_ORDER.get(row.get('level'), 3),
                                         row['days'] if row.get('days') is not None else 10 ** 6,
                                         row.get('module', ''), row.get('title', '')))


def normalize_summary(summary, rows):
    """把 reminders.summarize / health.summarize 的英文键统一成页面用的 total/高/中/低。"""
    summary = summary if isinstance(summary, dict) else {}
    counted = {'total': len(rows), '高': 0, '中': 0, '低': 0}
    for row in rows:
        level = row.get('level') if row.get('level') in LEVELS else '中'
        counted[level] += 1
    if summary:
        result = {'total': len(rows)}
        for label, key in SUMMARY_KEYS.items():
            if label == 'total':
                continue
            value = summary.get(key, summary.get(label, counted[label]))
            try:
                result[label] = int(value or 0)
            except (TypeError, ValueError):
                result[label] = counted[label]
    else:
        result = counted
    if sum(result[label] for label in LEVELS) != result['total']:
        result = counted
    for extra in ('scanned', 'checks'):
        if extra in summary:
            result[extra] = summary[extra]
    return result


def summarize_reminders(items):
    module = reminders_api()
    summarizer = getattr(module, 'summarize', None) if module is not None else None
    rows = [reminder_row(item) for item in (items or [])]
    if callable(summarizer):
        try:
            return normalize_summary(summarizer(items), rows)
        except Exception:
            pass
    return normalize_summary({}, rows)


def _reminder_record(self, row):
    record_id = str(row.get('record_id') or '')
    if not record_id:
        return None
    for record in getattr(self, 'records', []) or []:
        if str(record.get('id')) == record_id:
            return record
    return None


class _TaskTap:
    """按记录包装 client/runner：不改动 ActionCreationDialog，只旁路记录返回的任务 id。

    client 包装还会用“实际提交的 items”重新核对幂等键：内容未变沿用旧 request_id，
    内容变了（包括用户在任务表里逐项改写）必须换新的 request_id，避免服务端回放旧结果。
    """

    def __init__(self, runner, client, attempts):
        self.runner = runner
        self.client = client
        self.attempts = attempts
        self.by_record = {}
        self._record_id = None

    def for_record(self, record_id):
        self._record_id = record_id
        return self

    def submit(self, work, done, failed):
        return self.runner.submit(work, done, failed)

    def post(self, path, payload):
        record_id, attempt = self._record_id, None
        if path == '/actions/create' and record_id is not None and isinstance(payload, dict):
            attempt = self.attempts.setdefault(record_id, {})
            fingerprint = json.dumps(payload.get('items'), ensure_ascii=False, sort_keys=True)
            request_id = payload.get('request_id')
            if (attempt.get('fingerprint') != fingerprint
                    or attempt.get('request_id') != request_id):
                # 用户改过内容（或页面已换新键）：使用与当前内容绑定的新幂等键。
                request_id = attempt['request_id'] = uuid4().hex
            attempt['fingerprint'] = fingerprint
            payload = dict(payload, request_id=request_id)
        value = self.client.post(path, payload)
        if (path == '/actions/create' and record_id is not None and isinstance(value, dict)
                and attempt is not None and attempt.get('request_id') == payload.get('request_id')):
            for task in value.get('tasks', []):
                if isinstance(task, dict) and task.get('id'):
                    self.by_record.setdefault(record_id, []).append(str(task['id']))
        return value

    def tasks_for(self, record_id):
        return list(self.by_record.get(record_id, ()))


# --------------------------------------------------------------------------------------
# 体检：解析、筛选、导出
# --------------------------------------------------------------------------------------
def finding_row(finding):
    """把 health.scan 的一项映射为展示行；只读取权威键，其他内容一律忽略。"""
    finding = finding if isinstance(finding, dict) else {}
    return {
        'level': _level(finding, 'severity', 'level'),
        'module': _text(finding, 'module'),
        'title': _text(finding, 'title', default='未命名检查项'),
        'field': _text(finding, 'field'),
        'message': _text(finding, 'detail', 'message'),
        'advice': _text(finding, 'suggestion', 'advice'),
        'record_id': _text(finding, 'record_id'),
        'check': _text(finding, 'code', 'check'),
        'safe': {key: finding[key] for key in FINDING_KEYS if key in finding},
        'raw': finding,
    }


def sort_findings(rows):
    return sorted(rows, key=lambda row: (LEVEL_ORDER.get(row.get('level'), 3),
                                         row.get('module', ''), row.get('title', ''), row.get('field', '')))


def filter_findings(rows, level='', module=''):
    return [row for row in rows
            if (not level or row.get('level') == level) and (not module or row.get('module') == module)]


def finding_task_items(findings, limit=20):
    """整改任务项：优先用 health.finding_task_items，保证键顺序与文案完全一致。"""
    findings = list(findings or [])
    module = health_api()
    converter = getattr(module, 'finding_task_items', None) if module is not None else None
    if callable(converter):
        try:
            items = converter(findings, limit)
        except TypeError:
            items = converter(findings)
        if isinstance(items, list) and items:
            return [dict(item) for item in items]
    items = []
    for finding in findings[:max(0, int(limit))]:
        row = finding_row(finding)
        items.append({
            'name': f'整改：{row["title"]}（{row["field"] or row["check"] or "体检"}）'[:200],
            'owner': '',
            'due_date': (date.today() + timedelta(days=14)).isoformat(),
            'next_step': (row['advice'] or row['message'] or '核对体检发现并完成整改')[:500],
            'acceptance': '整改完成并留存证据',
        })
    return items


def health_summary(rows, scanned=None, checks=None):
    summary = normalize_summary({}, rows)
    summary['checks'] = int(checks) if isinstance(checks, int) else summary.get('checks', 0)
    if scanned is not None:
        summary['scanned'] = scanned
    return summary


def _finding_mapping(row_or_finding):
    if isinstance(row_or_finding, dict) and 'raw' in row_or_finding:
        return row_or_finding
    return finding_row(row_or_finding)


def _safe_export_text(value):
    """导出前兜底脱敏：任何凭据样式的文本都不写入导出文件。"""
    text = str(value if value is not None else '')
    for pattern in (r'(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|authorization)\b\s*[:=]\s*\S+',
                    r'(?i)\bBearer\s+\S+', r'\bsk-[A-Za-z0-9_\-]{6,}'):
        text = re.sub(pattern, '（已脱敏）', text)
    return text


def _export_cells(row_or_finding):
    """导出单元格只取白名单字段：不会带出任何其他内容（含潜在凭据）。"""
    row = _finding_mapping(row_or_finding)
    safe = row.get('safe') if isinstance(row.get('safe'), dict) else None
    if safe is None:
        safe = {key: row_or_finding[key] for key in FINDING_KEYS
                if isinstance(row_or_finding, dict) and key in row_or_finding}
    return [_safe_export_text(cell) for cell in (
        safe.get('severity', row.get('level', '')), _module_title(safe.get('module', '')) or safe.get('module', ''),
        safe.get('title', ''), safe.get('field', ''), safe.get('detail', ''), safe.get('suggestion', ''),
        safe.get('record_id', ''))]


def findings_to_csv(findings):
    """导出为 CSV 文本（含表头）；不包含任何密钥或凭据字段。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['等级', '模块', '事项', '字段', '问题说明', '建议', '记录ID'])
    for finding in findings or []:
        writer.writerow(_export_cells(finding))
    return buffer.getvalue()


def findings_to_markdown(findings):
    lines = ['# 台账数据体检结果', '',
             f'生成时间：{date.today().isoformat()} · 共 {len(list(findings or []))} 项发现', '',
             '> 本结果只读扫描生成，不构成合规结论；无证据不代表造假，只提示补齐。', '',
             '| 等级 | 模块 | 事项 | 字段 | 问题说明 | 建议 | 记录ID |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    for finding in findings or []:
        lines.append('| ' + ' | '.join(cell.replace('|', '\\|') for cell in _export_cells(finding)) + ' |')
    return '\n'.join(lines) + '\n'


def scan_records(records, today=None):
    """只读扫描：调用 health.scan；health 不可用时返回空结果并由页面明确提示。"""
    module = health_api()
    if module is not None and callable(getattr(module, 'scan', None)):
        return _call(module, 'scan', records, today if today is not None else date.today())
    return {'generated': date.today().isoformat(), 'scanned': 0, 'findings': [], 'summary': {'total': 0}}


def summarize_health(findings, scanned=None, checks=None):
    """把 health.summarize 的英文键统一成页面用的 total/高/中/低（+checks）。"""
    module = health_api()
    summarizer = getattr(module, 'summarize', None) if module is not None else None
    rows = [finding_row(finding) for finding in (findings or [])]
    raw = {}
    if callable(summarizer):
        try:
            raw = summarizer(findings)
        except Exception:
            raw = {}
    summary = normalize_summary(raw, rows)
    by_code = raw.get('by_code') if isinstance(raw.get('by_code'), dict) else {}
    default_checks = len(by_code) or int(raw.get('checks', 0) or 0)
    summary['checks'] = int(checks) if isinstance(checks, int) else default_checks
    if scanned is not None:
        summary['scanned'] = scanned
    return summary


# --------------------------------------------------------------------------------------
# 规则编辑对话框
# --------------------------------------------------------------------------------------
class RulesDialog(FormDialog):
    """提醒规则编辑器：字段名严格等于服务端 validate_rule 的 10 个键。

    modules / date_fields 是多值字段：表单里用复选框，保存时组装成列表。
    """

    def __init__(self, title, rule, client, runner, guard, on_success, parent=None):
        fields = rule_fields()
        values = rule_values(rule or {})
        choices = {'modules': reminder_module_choices()}
        super().__init__(title, fields, values, choices, parent)
        self.rule = dict(rule or {})
        self.client = client
        self.runner = runner
        self.guard = guard
        self.on_success = on_success
        self.saved = None
        self.module_boxes = {}
        self.field_boxes = {}
        self._build_multiselect(values)
        self._refresh_field_choices()
        chips = QLabel('日期字段必须是所选模块 schema 中真实存在的字段（含“日期”二字）；'
                       '保存前会用 reminders.validate_rule 校验，失败原因显示在本窗口内。')
        chips.setWordWrap(True)
        self.layout().insertWidget(2, chips)
        self._set_combo_data('type', RULE_TYPES_LABELS.get(values.get('type'), values.get('type')))

    def _set_combo_data(self, key, value):
        """按 userData 选中下拉项：FormDialog 对普通列表存的是选项文本本身。"""
        if key not in self.widgets:
            return
        combo = self.widgets[key][0]
        index = combo.findData(value)
        if index < 0:
            for position in range(combo.count()):
                if combo.itemData(position) == value or combo.itemText(position) == str(value):
                    index = position
                    break
        if index < 0 and value:
            combo.addItem(RULE_TYPES_LABELS.get(str(value), str(value)), value)
            index = combo.count() - 1
        combo.setCurrentIndex(max(0, index))

    def _build_multiselect(self, values):
        selected_modules = set(values.get('modules') or ['contracts'])
        selected_fields = set(values.get('date_fields') or [])
        all_modules = list(MODULES)
        box = QWidget()
        grid = QVBoxLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)
        choose_all = QCheckBox('全部模块（保存为全部模块列表）')
        choose_all.setObjectName('field_modules_all')
        choose_all.setChecked(selected_modules.issuperset(all_modules))
        grid.addWidget(choose_all)
        for key in all_modules:
            module_box = QCheckBox(MODULES[key][0])
            module_box.setObjectName('field_modules_' + key)
            module_box.setChecked(key in selected_modules)
            self.module_boxes[key] = module_box
            grid.addWidget(module_box)

        fields_box = QWidget()
        fields_grid = QVBoxLayout(fields_box)
        fields_grid.setContentsMargins(0, 0, 0, 0)
        for field in rule_time_fields(all_modules):
            field_box = QCheckBox(field)
            field_box.setObjectName('field_date_' + field)
            field_box.setChecked(field in selected_fields)
            self.field_boxes[field] = field_box
            fields_grid.addWidget(field_box)

        # 两个复选框字典都就绪后再接线，避免 setChecked/toggled 触发时字典还不完整。
        for module_box in self.module_boxes.values():
            module_box.toggled.connect(self._refresh_field_choices)
        choose_all.toggled.connect(self._sync_module_boxes)

        for key, widget in (('modules', box), ('date_fields', fields_box)):
            combo = self.widgets[key][0]
            row = self._rows.get(key)
            if row:
                self._rows[key] = (row[0], widget, row[2])
                row[0].addRow(QLabel(self._labels[key] + ' *'), widget)
            widget.setObjectName('field_' + key)
            combo.hide()
            combo.setParent(None)

    def _sync_module_boxes(self, checked):
        for key, box in self.module_boxes.items():
            box.blockSignals(True)
            box.setChecked(bool(checked))
            box.blockSignals(False)
        self._refresh_field_choices()

    def _refresh_field_choices(self, *_):
        allowed = rule_time_fields(self.selected_modules())
        for field, box in self.field_boxes.items():
            visible = field in allowed
            box.setVisible(visible)
            if not visible and box.isChecked():
                box.setChecked(False)

    def selected_modules(self):
        boxes = getattr(self, 'module_boxes', None)
        if not boxes:  # FormDialog.__init__ 会在子类控件建好前调用 values()
            return []
        chosen = [key for key, box in boxes.items() if box.isChecked()]
        return chosen or ['contracts']

    def selected_date_fields(self):
        return [field for field, box in getattr(self, 'field_boxes', {}).items() if box.isChecked()]

    def input_values(self):
        """把复选框选择合并进表单值；enabled 等其余字段原样保留。"""
        values = dict(super().values())
        values['modules'] = self.selected_modules() or values.get('modules') or ['contracts']
        fields = self.selected_date_fields()
        values['date_fields'] = fields or [str(item) for item in (values.get('date_fields') or [])]
        values['enabled'] = bool(self.widgets['enabled'][0].isChecked())
        return values

    def values(self):
        values = self.input_values()
        values.pop('id', None)
        values.pop('version', None)
        return values

    def validate(self):
        """规则校验交给 validate_rule（权威实现），失败原因显示在表单内。"""
        self.show_error('')
        try:
            validate_rule(rule_payload(self.input_values()))
        except (ValueError, TypeError) as exc:
            self.show_error(str(exc))
            return False
        return True

    def _request_payload(self, data):
        payload = {'data': rule_payload(data)}
        if self.rule.get('id'):
            payload['id'] = self.rule['id']
            payload['version'] = self.rule.get('version')
        validate_rule(payload['data'])
        return payload

    def save(self):
        if self.busy:
            return
        try:
            data = self.input_values()
            payload = self._request_payload(data)
        except (ValueError, TypeError) as exc:
            self.show_error(str(exc))
            return
        client, guard = self.client, self.guard
        self.set_busy(True)

        def work():
            return client.post('/reminders', payload)

        def done(value):
            # 页面已切走时不再触碰控件：只保留结果，避免操作已销毁的对象。
            if not guard():
                self.saved = value
                return
            self.set_busy(False)
            self.saved = value
            if self.on_success:
                self.on_success(value)
            self.accept()

        def failed(message):
            if not guard():
                return
            self.set_busy(False)
            self.show_error(str(message))

        self.runner.submit(work, done, failed)

    def _accept_validated(self):
        if not self.busy:
            self.save()


# --------------------------------------------------------------------------------------
# 提醒中心页面
# --------------------------------------------------------------------------------------
class InsightsMixin:
    # ------------------------------------------------------------------ 提醒中心
    def _insights_guard(self, page_key):
        client, generation = self.client, getattr(self, 'generation', 0)
        serial = getattr(self, '_insights_serial', 0)

        def valid():
            return (self.client is client and getattr(self, 'generation', 0) == generation
                    and getattr(self, 'current', None) == page_key and
                    getattr(self, '_insights_serial', 0) == serial)
        return valid

    def reminders_page(self):
        self._insights_serial = getattr(self, '_insights_serial', 0) + 1
        self._reminders_rules = []
        self._reminders_load_failed = False
        self._reminders_notice = ''
        layout = self.page_layout
        layout.addWidget(_hint(
            '提醒为程序内提示：不发送任何消息、不自动修改状态、也不代表负责人已读。'
            '“到期”按本机日期与记录日期计算，日期缺失的记录不会臆测到期时间。'))
        self.reminders_available = reminders_available()
        self.reminders_service_notice = _message_label(
            '' if self.reminders_available else UNAVAILABLE_REMINDERS_NOTICE)
        if self.reminders_service_notice.text():
            self.reminders_service_notice.setStyleSheet('color:#b42318;background:#fff1f0;padding:8px;border-radius:4px;')
        layout.addWidget(self.reminders_service_notice)
        self.reminders_message = _message_label(self._reminders_notice)
        layout.addWidget(self.reminders_message)
        self._reminders_stats_widget = QWidget()
        layout.addWidget(self._reminders_stats_widget)
        self.reminders_table = _table(REMINDER_COLUMNS)
        self.reminders_table.doubleClicked.connect(self._reminders_open)
        layout.addWidget(self.reminders_table, 2)
        state_tools = QHBoxLayout()
        layout.addLayout(state_tools)
        _button('标记已处理', lambda: self._reminders_set_state('done'), state_tools, True)
        _button('稍后提醒…', lambda: self._reminders_set_state('snooze'), state_tools)
        _button('忽略…', lambda: self._reminders_set_state('dismiss'), state_tools)
        self.reminders_show_handled = QCheckBox('显示已处理 / 已忽略（默认只看待处理）')
        self.reminders_show_handled.toggled.connect(lambda _: self._reminders_render_items())
        state_tools.addWidget(self.reminders_show_handled)
        state_tools.addStretch()
        layout.addWidget(_hint('已处理：不再出现在待处理清单。稍后提醒：到期后自动重新出现。忽略：需填写原因。'
                               '对应记录被修改后，处理状态自动失效并重新提醒；状态保存在本地工作库，可审计。'))
        self.reminders_empty = _message_label('')
        layout.addWidget(self.reminders_empty)
        self._reminders_render_items()
        self._reminders_rules_table = _table(['名称', '类型', '模块', '提前天数', '等级', '状态'])
        self._reminders_rules_table.doubleClicked.connect(lambda *_: self._reminders_edit())
        tools = QHBoxLayout()
        layout.addWidget(QLabel('提醒规则（保存在本地工作库）'))
        layout.addLayout(tools)
        _button('新建规则', self._reminders_create, tools, True)
        _button('编辑', self._reminders_edit, tools)
        _button('复制', self._reminders_copy, tools)
        _button('启用 / 停用', self._reminders_toggle, tools)
        _button('删除', self._reminders_delete, tools)
        _button('导入 JSON', self._reminders_import, tools)
        _button('导出 JSON', self._reminders_export, tools)
        _button('重新读取规则', self._reminders_load, tools)
        layout.addWidget(self._reminders_rules_table)
        self.reminders_rule_message = _message_label('')
        layout.addWidget(self.reminders_rule_message)
        self._reminders_load()

    def _reminders_render_items(self):
        """本地计算并渲染提醒清单；compute_items 报错时降级为空清单并提示。"""
        records = getattr(self, 'records', []) or []
        rows, items, error = [], [], ''
        if not self.reminders_available:
            error = UNAVAILABLE_REMINDERS_NOTICE
        else:
            try:
                module = reminders_api()
                rules = getattr(self, '_reminders_rules', None) or []
                if not rules:
                    rules = default_rules()[:0] if self._reminders_load_failed else default_rules()
                items = _rows_of(_call(module, 'compute_items', records, rules, date.today()))
                versions = {row.get('id'): row.get('version') for row in records if isinstance(row, dict)}
                for item in items:
                    if isinstance(item, dict) and item.get('record_id') in versions:
                        item['record_version'] = versions[item['record_id']]
                items = _apply_reminder_states(self, items)
                rows = sort_reminders([reminder_row(item) for item in items])
            except Exception as exc:  # 本地计算也必须降级而不是崩溃
                rows, items, error = [], [], '提醒计算失败：' + str(exc)
        self._reminder_rows = rows
        self._reminders_fill()
        self._render_reminder_stats(self._reminders_stats_widget, summarize_reminders(items))
        if error:
            self.reminders_message.setText(error)
        if not hasattr(self, 'reminders_empty'):
            return
        if rows:
            self.reminders_empty.setText('')
        else:
            # 空态必须仍然给出可操作引导（含服务降级时），而不是只显示错误。
            self.reminders_empty.setText(
                '暂无到期提醒，可先在合同 / 资产 / 权限记录中补齐日期（例如合同“到期日期”、资产“维保到期日期”、权限“复核日期”），'
                '再回到本页重新扫描。')

    def _render_reminder_stats(self, widget, summary):
        _reset_layout(widget)
        box = QHBoxLayout(widget)
        for title, key, hint in [('提醒总计', 'total', '仅统计已刷新数据'),
                                 ('高', '高', '需优先处理'), ('中', '中', '近期关注'), ('低', '低', '留意即可')]:
            box.addWidget(_stat_card(title, summary.get(key, 0), hint), 1)
        if summary.get('total'):
            box.addStretch()

    # ------------------------------------------------------------ 处理状态
    def _reminder_state_counts(self):
        rows = getattr(self, '_reminder_rows', []) or []
        handled = sum(1 for row in rows if row.get('handled'))
        return handled, len(getattr(self, '_reminder_states', []) or [])

    def _reminders_selected_item(self):
        table = getattr(self, 'reminders_table', None)
        if table is None or table.currentRow() < 0:
            return None
        item = table.item(table.currentRow(), 0)
        return item.data(Qt.UserRole) if item else None

    def _reminders_set_state(self, action):
        row = self._reminders_selected_item()
        if not row:
            self.reminders_message.setText('请先在提醒清单中选择一条提醒。')
            return
        # The visible row is a display mapping; the authoritative keys live in raw.
        raw = row.get('raw') if isinstance(row.get('raw'), dict) else row
        payload = {'rule_key': raw.get('rule_key', ''),
                   'record_id': raw.get('record_id') or row.get('record_id', ''),
                   'action': action, 'reason': '', 'record_version': raw.get('record_version')}
        if action == 'snooze':
            until, ok = QInputDialog.getText(self, '稍后提醒', '推迟到哪一天（YYYY-MM-DD，必须晚于今天）？',
                                             text=(date.today() + timedelta(days=7)).isoformat())
            if not ok:
                return
            payload['until'] = until.strip()
        elif action == 'dismiss':
            reason, ok = QInputDialog.getText(self, '忽略提醒', '忽略原因（必填，例如重复项、不适用）：')
            if not ok:
                return
            payload['reason'] = reason.strip()
        try:
            reminder_state().validate_state(payload, date.today())
        except ValueError as exc:
            self.reminders_message.setText(str(exc))
            return
        guard, client = self._reminders_guard(), self.client
        self.reminders_message.setText('正在保存处理状态…')

        def done(value):
            if not guard():
                return
            self.reminders_message.setText(
                {'done': '已标记为已处理。', 'snooze': '已推迟提醒。', 'dismiss': '已忽略该提醒。'}[action]
                + '记录内容发生变化后会自动重新提醒。')
            self._reminders_load()

        def failed(message):
            if guard():
                self.reminders_message.setText('保存处理状态失败：' + str(message))

        self.runner.submit(lambda: client.post('/reminders/state', {'data': payload}), done, failed)

    def _reminders_toggle_handled(self):
        self._reminders_render_items()

    def _reminders_fill(self):
        table = self.reminders_table
        rows = getattr(self, '_reminder_rows', [])
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            table.setItem(index, 0, _cell(row['level'], deepcopy(row)))
            table.setItem(index, 1, _cell(row['kind']))
            table.setItem(index, 2, _cell(_module_title(row['module']) or row['module']))
            table.setItem(index, 3, _cell(row['title']))
            table.setItem(index, 4, _cell(row['due_date']))
            table.setItem(index, 5, _cell(row['days_text']))
            table.setItem(index, 6, _cell(row['advice']))
        table.setSortingEnabled(True)

    def _reminders_open(self, *_):
        row = _selected(self.reminders_table)
        if not row:
            return
        record = _reminder_record(self, row)
        if record is None:
            # 记录不在当前已刷新数据中：忽略跳转，并说明原因。
            self.reminders_message.setText('该提醒对应的记录不在当前已刷新数据中，请先点击“刷新”后再试。')
            return
        self.edit_record(record.get('module'), record)

    # ------------------------------------------------------------------ 规则管理
    def _reminders_guard(self):
        return self._insights_guard(REMINDERS_NAV_KEY)

    def _reminders_load(self):
        guard, client = self._reminders_guard(), self.client
        runner = getattr(self, 'runner', None)
        if runner is None or client is None:  # 宿主未接线或页面正在拆除：降级展示内置规则
            self._reminders_degraded('提醒服务不可用')
            return
        self._reminders_serial = getattr(self, '_reminders_serial', 0) + 1
        serial = self._reminders_serial

        def work():
            try:
                states = client.get('/reminders/states')
            except Exception:
                states = []
            return client.get('/reminders'), states

        def done(value):
            if not guard() or serial != getattr(self, '_reminders_serial', 0):
                return
            rules_value, states_value = (value if isinstance(value, tuple) and len(value) == 2
                                         else (value, []))
            try:
                rules = _rows_of(rules_value)
            except ValueError as exc:
                self._reminders_degraded(str(exc))
                return
            self._reminders_rules = rules
            self._reminder_states = _rows_of(states_value) if not isinstance(states_value, Exception) else []
            self._reminders_load_failed = False
            self.reminders_rule_message.setText('' if rules else '当前工作库还没有提醒规则，可点击“新建规则”添加。')
            self._reminders_fill_rules()
            self._reminders_render_items()

        def failed(message):
            if guard() and serial == getattr(self, '_reminders_serial', 0):
                self._reminders_degraded(str(message))

        self.runner.submit(work, done, failed)

    def _reminders_degraded(self, message):
        """GET 失败：回退内置默认规则展示，并明确提示修改将失败。"""
        try:
            self._reminders_rules = default_rules()
        except Exception:
            self._reminders_rules = []
        self._reminders_load_failed = True
        self.reminders_rule_message.setText(f'规则读取失败（{message}）· {DEGRADED_RULES_NOTICE}')
        self._reminders_fill_rules()
        self._reminders_render_items()

    def _reminders_fill_rules(self):
        table = self._reminders_rules_table
        rules = getattr(self, '_reminders_rules', []) or []
        table.setRowCount(len(rules))
        for index, rule in enumerate(rules):
            values = rule_values(rule)
            modules = values.get('modules') or []
            module_text = '全部模块' if len(modules) > 1 else _module_title(modules[0] if modules else '')
            cells = [values.get('name', '未命名规则'), RULE_TYPES_LABELS.get(values.get('type'), values.get('type', '')),
                     module_text, values.get('warn_days', ''), values.get('severity', ''),
                     '已启用' if values.get('enabled', True) else '已停用']
            for column, text in enumerate(cells):
                table.setItem(index, column, _cell(text, deepcopy(rule)))

    def _reminders_selected_rule(self):
        rule = _selected(self._reminders_rules_table)
        if not rule:
            self.reminders_rule_message.setText('请先在规则列表中选择一条规则。')
            return None
        return rule

    def _reminders_rule_dialog(self, title, rule):
        return RulesDialog(title, rule, self.client, self.runner, self._reminders_guard(),
                           self._reminders_saved, self)

    def _reminders_create(self):
        dialog = self._reminders_rule_dialog('新建提醒规则', {})
        dialog.exec()

    def _reminders_edit(self):
        rule = self._reminders_selected_rule()
        if rule is None:
            return
        self._reminders_rule_dialog('编辑提醒规则', rule).exec()

    def _reminders_copy(self):
        rule = self._reminders_selected_rule()
        if rule is None:
            return
        copy = rule_values(rule)
        copy.pop('id', None)
        copy.pop('version', None)
        copy['name'] = str(copy.get('name', '')).strip() + '（副本）'
        copy['key'] = str(copy.get('key', 'rule')).strip() + '_copy'
        dialog = self._reminders_rule_dialog('复制为新规则', copy)
        dialog.rule = {}
        dialog.exec()

    def _reminders_saved(self, value):
        rule = rule_values(value if isinstance(value, dict) else {})
        name = rule.get('name') or '规则'
        self._reminders_load()  # 先重读（成功时会清空提示），再写保存结果
        self.reminders_rule_message.setText(f'已保存：{name}（版本 {rule.get("version", "-")}）。')

    def _reminders_toggle(self):
        rule = self._reminders_selected_rule()
        if rule is None:
            return
        values = rule_values(rule)
        if self._reminders_load_failed:
            self.reminders_rule_message.setText(DEGRADED_RULES_NOTICE)
            return
        values['enabled'] = not values.get('enabled', True)
        if QMessageBox.question(self, '启用 / 停用规则',
                                f'将规则“{values.get("name", "")}”设为' + ('启用' if values['enabled'] else '停用') + '？',
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        payload = {'data': rule_payload(values)}
        if rule.get('id'):
            payload.update(id=rule['id'], version=rule.get('version'))
        client = self.client

        def work():
            return client.post('/reminders', payload)

        def done(value):
            self._reminders_saved(value)

        def failed(message):
            self.reminders_rule_message.setText('保存失败：' + str(message))

        self.runner.submit(work, done, failed)

    def _reminders_delete(self):
        rule = self._reminders_selected_rule()
        if rule is None:
            return
        values = rule_values(rule)
        target = values.get('id')
        if target is None:
            if QMessageBox.question(self, '移除本地内置规则',
                                    f'内置默认规则“{values.get("name", "")}”没有服务端记录，只能从当前列表临时移除，'
                                    '重新读取后会再次出现。若要长期停用，请先保存为服务端规则再停用。是否继续？',
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            self._reminders_rules = [row for row in (getattr(self, '_reminders_rules', []) or [])
                                     if not (str(rule_values(row).get('name')) == str(values.get('name'))
                                             and rule_values(row).get('type') == values.get('type'))]
            self._reminders_fill_rules()
            self._reminders_render_items()
            self.reminders_rule_message.setText(f'已临时移除内置规则“{values.get("name", "")}”，重新读取后会再次出现。')
            return
        if QMessageBox.question(self, '删除提醒规则',
                                f'删除提醒规则“{values.get("name", "")}”？删除后不再产生该类提醒，'
                                '已经产生的业务记录不受影响；如需保留历史可改为停用。是否继续？',
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        valid, client = self._reminders_guard(), self.client
        self.reminders_rule_message.setText('正在删除提醒规则…')

        def done(value):
            if not valid():
                return
            self._reminders_rules = [row for row in (getattr(self, '_reminders_rules', []) or [])
                                     if str(rule_values(row).get('id')) != str(target)]
            self._reminders_fill_rules()
            self._reminders_render_items()
            self.reminders_rule_message.setText(f'已删除提醒规则“{values.get("name", "")}”。')

        def failed(message):
            if valid():
                self.reminders_rule_message.setText('删除失败：' + str(message))

        self.runner.submit(lambda: client.post('/reminders/delete', {'id': target, 'version': values.get('version')}),
                           done, failed)

    # ------------------------------------------------------------------ 导入 / 导出
    def _reminders_export(self):
        filename, _ = QFileDialog.getSaveFileName(self, '导出提醒规则 JSON', 'reminder-rules.json', 'JSON (*.json)')
        if not filename:
            return
        payload = {'kind': 'reminder-rules', 'exported': date.today().isoformat(),
                   'rules': [rule_payload(rule_values(rule)) for rule in (getattr(self, '_reminders_rules', []) or [])]}
        try:
            with open(filename, 'w', encoding='utf-8') as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        except OSError as exc:
            self.reminders_rule_message.setText('导出失败：' + str(exc))
            return
        self.reminders_rule_message.setText(f'已导出 {len(payload["rules"])} 条规则到 {filename}。')

    def _reminders_import(self):
        filename, _ = QFileDialog.getOpenFileName(self, '导入提醒规则 JSON', '', 'JSON (*.json)')
        if not filename:
            return
        try:
            with open(filename, 'r', encoding='utf-8-sig') as handle:
                value = json.loads(handle.read())
        except (OSError, ValueError) as exc:
            self.reminders_rule_message.setText('导入失败：无法解析 JSON 数据（' + str(exc) + '）')
            return
        candidates, errors = self._reminders_candidates(value)
        if not candidates:
            self.reminders_rule_message.setText('导入失败：JSON 中没有任何规则对象。' + (' ' + '；'.join(errors) if errors else ''))
            return
        if not self._reminders_preview(candidates, errors):
            self.reminders_rule_message.setText('已取消导入，未发送任何请求。')
            return
        client = self.client
        payloads = [{'data': rule_payload(rule)} for rule in candidates]
        self.reminders_rule_message.setText('正在导入 ' + str(len(payloads)) + ' 条规则…')

        def work():
            return [client.post('/reminders', payload) for payload in payloads]

        def done(value):
            self._reminders_load()

        def failed(message):
            self.reminders_rule_message.setText('导入失败：' + str(message))

        self.runner.submit(work, done, failed)

    @staticmethod
    def _reminders_candidates(value):
        """只接受 JSON 数据；不执行任何代码，逐条校验。"""
        if isinstance(value, dict) and isinstance(value.get('rules'), list):
            raw = value['rules']
        elif isinstance(value, list):
            raw = value
        elif isinstance(value, dict):
            raw = [value]
        else:
            return [], ['需要 JSON 对象或数组']
        candidates, errors = [], []
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                errors.append(f'第{index + 1}条不是一个JSON对象，已跳过')
                continue
            try:
                candidate = rule_payload(rule_values(entry))
                validate_rule_local(candidate)  # 导入前先按权威结构校验，避免把无效规则发出去
            except (ValueError, TypeError) as exc:
                errors.append(f'第{index + 1}条：{exc}')
                continue
            candidates.append(candidate)
        return candidates, errors

    def _reminders_preview(self, candidates, errors):
        preview = json.dumps(candidates, ensure_ascii=False, indent=2)
        if len(preview) > 40000:
            preview = preview[:40000] + '\n…（内容过长，仅展示前 40000 字符）'
        note = f'将导入 {len(candidates)} 条规则（仅作为 JSON 数据解析，不执行任何代码）。'
        if errors:
            note += ' 已跳过 ' + str(len(errors)) + ' 条无效数据：' + '；'.join(errors)
        dialog = FormDialog('导入预览：核对规则后确认', [('preview', note + '（只读预览）', 'long', '')],
                            {'preview': preview}, parent=self)
        dialog.widgets['preview'][0].setReadOnly(True)
        dialog._required = set()
        button = dialog.buttons.button(QDialogButtonBox.Save)
        if button is not None:
            button.setText('确认导入')
        return dialog.exec() == QDialog.Accepted

    # ------------------------------------------------------------------ 体检页面
    def health_page(self):
        self._insights_serial = getattr(self, '_insights_serial', 0) + 1
        self._health_attempts = {}  # 内容不变的失败重试要沿用同一 request_id（跨切页保留）
        layout = self.page_layout
        layout.addWidget(_hint(
            '体检只读扫描，不修改任何记录；发现的“建议”需要人工确认后才会建立整改任务。'
            '无证据不等于造假，只提示补齐；' + HEALTH_DISCLAIMER))
        self.health_available = health_available()
        self.health_service_notice = _message_label('' if self.health_available else UNAVAILABLE_HEALTH_NOTICE)
        if self.health_service_notice.text():
            self.health_service_notice.setStyleSheet('color:#b42318;background:#fff1f0;padding:8px;border-radius:4px;')
        layout.addWidget(self.health_service_notice)
        self.health_message = _message_label('')
        layout.addWidget(self.health_message)
        self._health_scan()
        self._health_stats_widget = QWidget()
        layout.addWidget(self._health_stats_widget)
        self._health_stats()
        filters = QHBoxLayout()
        layout.addLayout(filters)
        self.health_level_filter = QComboBox()
        self.health_level_filter.addItem('全部等级', '')
        for level in LEVELS:
            self.health_level_filter.addItem(level, level)
        self.health_module_filter = QComboBox()
        self.health_module_filter.addItem('全部模块', '')
        for key in sorted({row['module'] for row in self._health_rows if row['module']}):
            self.health_module_filter.addItem(_module_title(key), key)
        filters.addWidget(QLabel('筛选'))
        filters.addWidget(self.health_level_filter)
        filters.addWidget(self.health_module_filter)
        filters.addStretch()
        _button('全选当前筛选', lambda: self._health_check_all(True), filters)
        _button('全部取消', lambda: self._health_check_all(False), filters)
        self.health_table = _table(HEALTH_COLUMNS, checkable=True)
        layout.addWidget(self.health_table, 2)
        self._health_fill()
        self.health_empty = _message_label('')
        layout.addWidget(self.health_empty)
        self._health_update_empty()
        actions = QHBoxLayout()
        layout.addLayout(actions)
        self.health_create_button = _button('建立整改任务…', self._health_create_tasks, actions, True)
        _button('标记已整改', lambda: self._health_set_state('resolved'), actions)
        _button('误报忽略…', lambda: self._health_set_state('ignored'), actions)
        self.health_show_handled = QCheckBox('显示已处理（已整改 / 已转任务 / 误报）')
        self.health_show_handled.toggled.connect(lambda _: self._health_rescan())
        actions.addWidget(self.health_show_handled)
        _button('导出体检结果…', self._health_export, actions)
        _button('重新扫描', self._health_rescan, actions)
        actions.addStretch()
        layout.addWidget(_message_label('勾选状态默认为“全不选”：请逐项确认后再建立整改任务，建立前仍可在任务表中修改内容。'
                                        '已处理的体检项默认不再重复报警；对应记录被修改后会自动重新出现。'))
        self.health_task_message = _message_label('')
        layout.addWidget(self.health_task_message)
        self._health_load_states()

    def _health_load_states(self):
        guard, client = self._insights_guard(HEALTH_NAV_KEY), self.client
        runner = getattr(self, 'runner', None)
        if runner is None or client is None:
            return

        def work():
            return client.get('/health/states')

        def done(value):
            if not guard():
                return
            self._health_states = _rows_of(value)
            self._health_rescan()

        def failed(message):
            if guard():
                self._health_states = []

        runner.submit(work, done, failed)

    def _health_selected_finding(self):
        table = getattr(self, 'health_table', None)
        if table is None or table.currentRow() < 0:
            return None
        item = table.item(table.currentRow(), 0)
        row = item.data(Qt.UserRole) if item else None
        if not isinstance(row, dict):
            return None
        return row.get('raw') if isinstance(row.get('raw'), dict) else row

    def _health_set_state(self, status):
        finding = self._health_selected_finding()
        if not finding:
            self.health_task_message.setText('请先在体检清单中选择一项。')
            return
        payload = {'code': finding.get('code', ''), 'record_id': finding.get('record_id', ''),
                   'field': finding.get('field', ''), 'status': status, 'reason': '',
                   'record_version': finding.get('record_version')}
        if status == 'ignored':
            reason, ok = QInputDialog.getText(self, '误报忽略', '忽略原因（必填，例如字段不适用、已由其他制度覆盖）：')
            if not ok:
                return
            payload['reason'] = reason.strip()
        try:
            health_state().validate_state(payload)
        except ValueError as exc:
            self.health_task_message.setText(str(exc))
            return
        self._health_save_state(payload)

    def _health_save_state(self, payload):
        guard, client = self._insights_guard(HEALTH_NAV_KEY), self.client

        def done(value):
            if not guard():
                return
            labels = {'resolved': '已标记为已整改。', 'ignored': '已标记为误报忽略。',
                      'task_created': '已记录整改任务关联。'}
            self.health_task_message.setText(labels.get(payload['status'], '状态已保存。')
                                             + '记录被修改后会自动重新出现。')
            self._health_load_states()

        def failed(message):
            if guard():
                self.health_task_message.setText('保存处理状态失败：' + str(message))

        self.runner.submit(lambda: client.post('/health/state', {'data': payload}), done, failed)

    def _health_scan(self):
        records = getattr(self, 'records', []) or []
        if not self.health_available:
            self._health_findings, self._health_rows = [], []
            self._health_scanned, self._health_checks = 0, 0
            self._health_generated = date.today().isoformat()
            if hasattr(self, 'health_message'):
                self.health_message.setText(UNAVAILABLE_HEALTH_NOTICE)
            return
        try:
            result = scan_records(records, date.today())
        except Exception as exc:
            self._health_findings, self._health_scanned, self._health_checks = [], 0, 0
            self._health_generated = date.today().isoformat()
            self._health_rows = []
            if hasattr(self, 'health_message'):
                self.health_message.setText('体检扫描失败：' + str(exc))
            return
        result = result if isinstance(result, dict) else {}
        findings = _rows_of(result.get('findings', []))
        versions = {row.get('id'): row.get('version') for row in records if isinstance(row, dict)}
        for finding in findings:
            if isinstance(finding, dict) and finding.get('record_id') in versions:
                finding['record_version'] = versions[finding['record_id']]
        all_findings = list(findings)
        findings = _apply_health_states(self, findings)
        summary = summarize_health(all_findings, result.get('scanned'), None)
        self._health_findings = findings
        self._health_scanned = summary.get('scanned', result.get('scanned', len(records)))
        self._health_checks = summary.get('checks', 0)
        self._health_generated = str(result.get('generated') or date.today().isoformat())
        self._health_rows = sort_findings([finding_row(finding) for finding in findings])
        if hasattr(self, 'health_message'):
            self.health_message.setText('')

    def _health_stats(self):
        rows = getattr(self, '_health_rows', [])
        summary = health_summary(rows, self._health_scanned, self._health_checks)
        widget = self._health_stats_widget
        _reset_layout(widget)
        box = QHBoxLayout(widget)
        for title, value in [('发现总计', summary['total']), ('高', summary.get('高', 0)),
                             ('中', summary.get('中', 0)), ('低', summary.get('低', 0)),
                             ('命中检查项', summary.get('checks', 0)),
                             ('扫描记录数', summary.get('scanned', 0))]:
            box.addWidget(_stat_card(title, value), 1)
        meta = QLabel(f'生成时间：{getattr(self, "_health_generated", "")}（只读扫描结果）')
        meta.setWordWrap(True)
        box.addWidget(meta, 2)

    def _health_visible_rows(self):
        level = self.health_level_filter.currentData() if hasattr(self, 'health_level_filter') else ''
        module = self.health_module_filter.currentData() if hasattr(self, 'health_module_filter') else ''
        return filter_findings(getattr(self, '_health_rows', []), level or '', module or '')

    def _health_fill(self):
        table = self.health_table
        rows = self._health_visible_rows()
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            first = _cell(row['level'], deepcopy(row), checkable=True)
            table.setItem(index, 0, first)
            for column, value in enumerate((_module_title(row['module']) or row['module'], row['title'],
                                            row['field'], row['message'], row['advice']), start=1):
                table.setItem(index, column, _cell(value))
        table.setSortingEnabled(True)
        self._health_update_empty()

    def _health_update_empty(self):
        # Page attributes survive a rebuild while their C++ widgets may already be
        # gone, so both the attribute check and setText must tolerate a stale wrapper.
        if not _alive(getattr(self, 'health_empty', None)):
            return
        rows, total = self._health_visible_rows(), getattr(self, '_health_rows', [])
        notice = getattr(self, 'health_service_notice', None)
        if rows:
            self._health_empty_text('')
        elif total:
            self._health_empty_text('当前筛选没有匹配的发现；请调整等级或模块筛选条件。')
        elif not getattr(self, 'health_available', True):
            self._health_empty_text(UNAVAILABLE_HEALTH_NOTICE)
        elif _alive(notice) and notice.text():
            self._health_empty_text(notice.text())
        else:
            self._health_empty_text('未发现明显问题。' + HEALTH_DISCLAIMER)

    def _health_empty_text(self, text):
        widget = getattr(self, 'health_empty', None)
        try:
            if widget is not None:
                widget.setText(text)
        except RuntimeError:
            pass

    def _health_rescan(self):
        self._health_scan()
        current = self.health_module_filter.currentData() if hasattr(self, 'health_module_filter') else ''
        if hasattr(self, 'health_module_filter'):
            self.health_module_filter.blockSignals(True)
            self.health_module_filter.clear()
            self.health_module_filter.addItem('全部模块', '')
            for key in sorted({row['module'] for row in self._health_rows if row['module']}):
                self.health_module_filter.addItem(_module_title(key), key)
            index = self.health_module_filter.findData(current)
            self.health_module_filter.setCurrentIndex(max(0, index))
            self.health_module_filter.blockSignals(False)
        self._health_stats()
        self._health_fill()

    def _health_check_all(self, checked):
        for index in range(self.health_table.rowCount()):
            item = self.health_table.item(index, 0)
            if item is not None:
                item.setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def _health_checked(self):
        rows = []
        for index in range(self.health_table.rowCount()):
            item = self.health_table.item(index, 0)
            if item is not None and item.checkState() == Qt.Checked:
                rows.append(item.data(Qt.UserRole) or {})
        return rows

    def _health_create_tasks(self):
        checked = self._health_checked()
        if not checked:
            self.health_task_message.setText('请先勾选需要整改的发现项；默认全不选，避免把提示误当成结论。')
            return
        groups, skipped = {}, []
        for row in checked:
            record_id = str(row.get('record_id') or '')
            if not record_id:
                skipped.append(row.get('title', ''))
                continue
            groups.setdefault(record_id, []).append(row['raw'] if isinstance(row.get('raw'), dict) else row)
        if not groups:
            self.health_task_message.setText('勾选的发现项没有关联业务记录（记录ID为空），无法建立整改任务；请先补齐记录或改用记录内的整改字段。')
            return
        total = sum(len(items) for items in groups.values())
        header = ('无证据不等于造假：本次只为已勾选的发现建立“待执行任务”，不发送通知、不视为审批。\n'
                  f'将按 {len(groups)} 条记录分组、共 {total} 项任务，逐组人工确认。\n是否继续？')
        if skipped:
            header += '\n（已跳过 ' + str(len(skipped)) + ' 项无记录ID的发现）'
        if QMessageBox.question(self, '确认建立整改任务', header,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._health_submit_groups(groups)

    def _health_submit_groups(self, groups):
        if ActionCreationDialog is None:  # pragma: no cover - 极端降级
            self.health_task_message.setText('无法建立整改任务：缺少 ActionCreationDialog 依赖。')
            return
        guard, client = self._insights_guard(HEALTH_NAV_KEY), self.client
        attempts = getattr(self, '_health_attempts', None)
        if attempts is None:
            attempts = self._health_attempts = {}
        # 不修改 ActionCreationDialog：用一个 client/runner 代理同时旁路任务 id 与幂等键。
        tap = _TaskTap(self.runner, client, attempts)
        created, warnings = [], []
        for record_id, findings in groups.items():
            items = finding_task_items(findings)
            if not items:
                continue
            fingerprint = json.dumps(items, ensure_ascii=False, sort_keys=True)
            attempt = attempts.get(record_id)
            if not attempt or attempt.get('fingerprint') != fingerprint:
                # 内容改变（或首次）使用新的幂等键；内容不变的重试沿用同一 request_id。
                attempts[record_id] = attempt = {'fingerprint': fingerprint, 'request_id': uuid4().hex}
            tap.for_record(record_id)
            dialog = ActionCreationDialog(record_id, items, tap, tap, self)
            dialog.request_id = attempt['request_id']
            dialog.exec()
            task_ids = tap.tasks_for(record_id)
            if task_ids:
                created.extend(task_ids)
            else:
                warnings.append(record_id)
            if not guard() or self.client is not client:
                break
        if not guard() or self.client is not client:
            return
        if created:
            self.health_task_message.setText('已建立整改任务：' + json.dumps([str(x) for x in created], ensure_ascii=False))
            self._health_mark_task_created(groups, tap)
        elif warnings:
            self.health_task_message.setText('未建立任务（已取消或未确认）：' + '、'.join(warnings) +
                                             '。勾选与编辑内容已保留，可重试；内容不变时沿用同一请求编号。')
        else:
            self.health_task_message.setText('未建立任何任务；勾选状态已保留。')
        if created and hasattr(self, 'refresh'):
            self.refresh()  # 只有真的建立了任务才刷新，失败/取消不打断用户

    def _health_mark_task_created(self, groups, tap):
        """Record task_created for the findings that actually produced a task."""
        client = self.client
        guard = self._insights_guard(HEALTH_NAV_KEY)
        payloads = []
        for record_id, findings in groups.items():
            task_ids = tap.tasks_for(record_id)
            if not task_ids:
                continue
            for finding in findings:
                payloads.append({'code': finding.get('code', ''), 'record_id': finding.get('record_id', ''),
                                 'field': finding.get('field', ''), 'status': 'task_created',
                                 'reason': '已建立整改任务', 'record_version': finding.get('record_version'),
                                 'task_id': str(task_ids[0])})
        if not payloads:
            return
        try:
            for payload in payloads:
                health_state().validate_state(payload)
        except (ValueError, Exception):
            return  # 状态仅用于减少重复提醒，失败不影响已建立的任务

        def work():
            results = []
            for payload in payloads:
                try:
                    results.append(client.post('/health/state', {'data': payload}))
                except Exception:
                    continue
            return results

        def done(value):
            if guard():
                self._health_load_states()

        self.runner.submit(work, done, lambda message: None)

    # ------------------------------------------------------------------ 导出
    def _health_export(self):
        filename, selected = QFileDialog.getSaveFileName(self, '导出体检结果', '台账数据体检.csv', EXPORT_FILTERS)
        if not filename:
            return
        markdown = filename.lower().endswith('.md') or filename.lower().endswith('.markdown') or 'Markdown' in (selected or '')
        if not filename.lower().endswith(('.csv', '.md', '.markdown', '.txt')):
            filename += '.md' if markdown else '.csv'
        rows = self._health_visible_rows()
        content = findings_to_markdown(rows) if markdown else findings_to_csv(rows)
        try:
            with open(filename, 'w', encoding='utf-8-sig' if not markdown else 'utf-8', newline='') as handle:
                handle.write(content)
        except OSError as exc:
            self.health_task_message.setText('导出失败：' + str(exc))
            return
        self.health_task_message.setText(f'已导出 {len(rows)} 项发现到 {filename}（不含密钥与凭据）。')
