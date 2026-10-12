"""Pure reminder rules over stored records: no IO, no network, no clock, no writes.

Rules are plain JSON-serializable dictionaries; every failure uses a fixed
message from SAFE_REMINDER_ERRORS without interpolating user values. Callers
pass the server ``date.today()`` explicitly so results stay reproducible.
"""
from __future__ import annotations

import re
from datetime import date

from .schema import MODULES

ENDED_STATUSES = frozenset({'已完成', '已取消', '已关闭', '已定稿'})
SEVERITIES = ('高', '中', '低')
RULE_TYPES = ('expiry', 'overdue', 'stale')

# Fixed user-facing errors; never interpolate untrusted values into exceptions.
SAFE_REMINDER_ERRORS = frozenset({
    '提醒规则名称无效', '提醒规则类型无效', '提醒规则的业务模块无效',
    '提醒规则日期字段不符合模块定义', '提醒提前天数须为0—365',
    '提醒未更新天数须为1—3650', '提醒等级无效', '提醒规则包含未知字段',
    '提醒规则启用状态须为布尔值', '提醒规则key无效',
    '提醒输入记录格式无效', '提醒规则结构无效', '提醒扫描日期无效',
})

_RULE_KEYS = frozenset({'key', 'name', 'type', 'modules', 'date_fields', 'warn_days',
                        'stale_days', 'severity', 'enabled', 'note'})
# Storage adds these when a rule is persisted; they are metadata, not rule content.
STORAGE_KEYS = frozenset({'id', 'version', 'created', 'updated', 'oldhistory'})
_KEY = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}')
_SEVERITY_RANK = {'高': 0, '中': 1, '低': 2}
_MAX_NAME = 200
_MAX_NOTE = 1000


def _rule(key, name, kind, modules, date_fields, warn_days, stale_days, severity, note):
    """Build one rule in the documented key order (fresh lists, never shared)."""
    return {'key': key, 'name': name, 'type': kind, 'modules': list(modules),
            'date_fields': list(date_fields), 'warn_days': warn_days,
            'stale_days': stale_days, 'severity': severity, 'enabled': True, 'note': note}


def default_rules() -> list[dict]:
    """Return fresh built-in rules using real schema field names."""
    return [
        _rule('contract_expiry', '合同到期提醒', 'expiry', ['contracts'], ['到期日期'],
              60, 365, '高', '合同到期前60天提醒；同时人工核对通知截止日期、SLA与退出安排。'),
        _rule('asset_warranty_expiry', '资产维保到期提醒', 'expiry', ['assets'], ['维保到期日期'],
              30, 730, '高', '维保到期前30天提醒，确认续保、替换或退役方案与预算。'),
        _rule('access_review_expiry', '权限复核到期提醒', 'expiry', ['access'], ['复核日期'],
              30, 365, '高', '按复核日期提醒周期性权限复核，保留审批依据与整改任务。'),
        _rule('recovery_drill_expiry', '备份恢复演练提醒', 'expiry', ['recovery'], ['演练日期'],
              30, 365, '中', '演练计划日期临近或已过时提醒；演练结论与实际耗时须人工确认。'),
        _rule('review_cycle_expiry', '考核周期届满提醒', 'expiry', ['reviews'], ['考核周期'],
              14, 365, '中', '考核周期填写为YYYY-MM-DD（或以其开头的区间）时按该日期判定届满；文字周期需人工换算为日期。'),
        _rule('risk_review_expiry', '风险复核到期提醒', 'expiry', ['risks'], ['复核日期'],
              14, 365, '中', '风险复核日期到期提醒；长期未复核的风险应重新评估等级与措施。'),
        _rule('task_overdue', '任务逾期提醒', 'overdue', ['tasks'], ['截止日期'],
              0, 180, '高', '截止日期已过且任务未结束；已完成、已取消、已关闭任务不提醒。'),
        _rule('stale_open_record', '长期未更新提醒', 'stale', list(MODULES), [],
              0, 180, '低', '未结束记录超过180天未更新时提醒，请更新进展或归档。'),
    ]


def validate_rule(data) -> None:
    """Raise ValueError with a fixed SAFE_REMINDER_ERRORS message, or return None."""
    if not isinstance(data, dict) or set(data) - _RULE_KEYS:
        raise ValueError('提醒规则包含未知字段')
    key = data.get('key')
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise ValueError('提醒规则key无效')
    name = data.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > _MAX_NAME:
        raise ValueError('提醒规则名称无效')
    kind = data.get('type')
    if not isinstance(kind, str) or kind not in RULE_TYPES:
        raise ValueError('提醒规则类型无效')
    modules = data.get('modules')
    if (not isinstance(modules, list) or not modules
            or any(not isinstance(item, str) or item not in MODULES for item in modules)
            or len(set(modules)) != len(modules)):
        raise ValueError('提醒规则的业务模块无效')
    date_fields = data.get('date_fields')
    if (not isinstance(date_fields, list)
            or any(not isinstance(item, str) for item in date_fields)
            or len(set(date_fields)) != len(date_fields)):
        raise ValueError('提醒规则日期字段不符合模块定义')
    allowed = {field for module in modules for field in MODULES[module][1]}
    if any(field not in allowed for field in date_fields):
        raise ValueError('提醒规则日期字段不符合模块定义')
    if kind == 'expiry' and not date_fields:
        raise ValueError('提醒规则日期字段不符合模块定义')
    warn_days = data.get('warn_days')
    if type(warn_days) is not int or not 0 <= warn_days <= 365:
        raise ValueError('提醒提前天数须为0—365')
    stale_days = data.get('stale_days')
    if type(stale_days) is not int or not 1 <= stale_days <= 3650:
        raise ValueError('提醒未更新天数须为1—3650')
    severity = data.get('severity')
    if not isinstance(severity, str) or severity not in SEVERITIES:
        raise ValueError('提醒等级无效')
    if type(data.get('enabled')) is not bool:
        raise ValueError('提醒规则启用状态须为布尔值')
    note = data.get('note', '')
    if not isinstance(note, str) or len(note) > _MAX_NOTE:
        raise ValueError('提醒规则包含未知字段')


def _parse_date(value):
    """Parse the leading YYYY-MM-DD of a text value; return None when unusable."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _worse(severity, floor):
    return severity if _SEVERITY_RANK[severity] <= _SEVERITY_RANK[floor] else floor


def _item(rule, module, record_id, title, day, days_left, message, severity=None):
    severity = severity or rule['severity']
    return {'rule_key': rule['key'], 'rule_name': rule['name'], 'severity': severity,
            'module': module, 'record_id': record_id, 'title': title,
            'date': day.isoformat(), 'days_left': days_left,
            'overdue_days': max(0, -days_left), 'message': message,
            'sort': [_SEVERITY_RANK[severity], days_left, module, title]}


def _rule_items(rule, row, module, data, record_id, title, today):
    kind = rule['type']
    if kind == 'stale':
        updated = _parse_date(row.get('updated'))
        if updated is None:
            return []
        age = (today - updated).days
        if age <= rule['stale_days']:
            return []
        return [_item(rule, module, record_id, title, updated, -age,
                      '最后更新 %s，已 %d 天未更新' % (updated.isoformat(), age))]
    fields = rule['date_fields'] or (['截止日期'] if kind == 'overdue' else [])
    items = []
    for field in fields:
        due = _parse_date(data.get(field))
        if due is None:
            continue
        days_left = (due - today).days
        if kind == 'expiry':
            if days_left > rule['warn_days']:
                continue
            severity = rule['severity']
            if days_left < 0:
                severity = '高' if days_left < -30 else _worse(rule['severity'], '中')
            message = ('%s %s 已过期 %d 天，请尽快处理' % (field, due.isoformat(), -days_left)
                       if days_left < 0 else
                       '%s %s 还有 %d 天到期' % (field, due.isoformat(), days_left))
        else:
            if days_left >= 0:
                continue
            severity = rule['severity']
            message = '%s %s 已逾期 %d 天，记录仍未结束' % (field, due.isoformat(), -days_left)
        items.append(_item(rule, module, record_id, title, due, days_left, message, severity))
    return items


def _better(candidate, current):
    """Highest severity first, then the nearest date (smallest days_left)."""
    left, right = _SEVERITY_RANK[candidate['severity']], _SEVERITY_RANK[current['severity']]
    if left != right:
        return left < right
    return candidate['days_left'] < current['days_left']


def compute_items(records, rules, today) -> list[dict]:
    """Compute reminder items; archived, finished and malformed rows are skipped."""
    if type(today) is not date:
        raise ValueError('提醒扫描日期无效')
    if not isinstance(records, list):
        raise ValueError('提醒输入记录格式无效')
    if not isinstance(rules, list):
        raise ValueError('提醒规则结构无效')
    active = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        # Rules read back from storage carry version/audit metadata; validate the
        # rule content only, otherwise every stored rule would be silently skipped.
        content = {key: value for key, value in rule.items() if key not in STORAGE_KEYS}
        try:
            validate_rule(content)
        except ValueError:
            continue
        if content['enabled']:
            active.append(content)
    items = []
    for row in records:
        if not isinstance(row, dict) or row.get('archived'):
            continue
        module, data, record_id = row.get('module'), row.get('data'), row.get('id')
        if (not isinstance(module, str) or module not in MODULES
                or not isinstance(data, dict)
                or not isinstance(record_id, str) or not record_id.strip()):
            continue
        status = data.get('状态')
        if isinstance(status, str) and status in ENDED_STATUSES:
            continue
        title = data.get(MODULES[module][1][0], '')
        title = title.strip() if isinstance(title, str) else ''
        best = None
        for rule in active:
            if module not in rule['modules']:
                continue
            for item in _rule_items(rule, row, module, data, record_id, title, today):
                if best is None or _better(item, best):
                    best = item
        if best is not None:
            items.append(best)
    items.sort(key=lambda item: item['sort'])
    return items


def summarize(items) -> dict:
    """Count items by severity, module and rule key."""
    result = {'total': 0, 'high': 0, 'medium': 0, 'low': 0, 'by_module': {}, 'by_rule': {}}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        result['total'] += 1
        severity = item.get('severity')
        if severity == '高':
            result['high'] += 1
        elif severity == '中':
            result['medium'] += 1
        elif severity == '低':
            result['low'] += 1
        module, rule_key = item.get('module'), item.get('rule_key')
        if isinstance(module, str):
            result['by_module'][module] = result['by_module'].get(module, 0) + 1
        if isinstance(rule_key, str):
            result['by_rule'][rule_key] = result['by_rule'].get(rule_key, 0) + 1
    return result
