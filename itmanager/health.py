"""Read-only data-health analysis over stored records: no writes, no IO, no network.

Findings are produced only when the supplied records actually trigger a check;
nothing is inferred from titles, owners or free text. Every failure uses a fixed
message from SAFE_HEALTH_ERRORS without interpolating user values.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .schema import MODULES
from .store import REFERENCES

# Fixed user-facing errors; never interpolate untrusted values into exceptions.
SAFE_HEALTH_ERRORS = frozenset({
    '体检输入记录格式无效', '体检扫描日期无效', '体检整改项数量无效',
})

ENDED_STATUSES = frozenset({'已完成', '已取消', '已关闭', '已定稿'})
OWNER_FIELDS = ('负责人', '责任人', '业务负责人', '技术负责人', '项目负责人', '主管', '复核人')
KEY_DATES = {
    'plans': ('截止日期',), 'projects': ('截止日期',), 'tasks': ('截止日期',),
    'collection': ('截止日期',), 'decisions': ('截止日期',), 'risks': ('复核日期',),
    'contracts': ('到期日期',), 'assets': ('维保到期日期',), 'access': ('复核日期',),
    'recovery': ('演练日期',),
}
UNIQUE_FIELDS = {'assets': ('资产编号',), 'systems': ('名称',), 'contracts': ('名称',)}
SYSTEM_REQUIRED = ('RTO小时', 'RPO小时', '关键性')
REVIEW_REQUIRED = ('考核周期', '指标口径')
STALE_DAYS = 180
REMEDY_DAYS = 14
MAX_REMEDY_ITEMS = 20
_SEVERITY_RANK = {'高': 0, '中': 1, '低': 2}

_SUGGESTIONS = {
    'missing_owner': '请指定%s并确认其已知悉该职责。',
    'missing_date': '请补充%s，无明确日期时先标注待确认。',
    'missing_evidence': '请补充%s或说明缺口，证据须人工核验。',
    'duplicate_number': '请核对原始资料，保留唯一编号，重复记录合并或归档。',
    'overdue_unhandled': '请确认延期或关闭，并建立整改任务与验收标准。',
    'orphan_reference': '请重新选择有效的关联记录或清空该字段。',
    'review_overdue': '请按复核日期完成复核并留存结论。',
    'stale_open': '请更新进展、下一步与截止日期，或归档该记录。',
    'system_no_recovery': '请与业务确认%s并写入台账。',
    'review_missing_cycle': '请补充%s，确保考核口径可核验。',
}


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _parse_date(value):
    """Parse the leading YYYY-MM-DD of a text value; return None when unusable."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _title(module, data):
    value = data.get(MODULES[module][1][0], '')
    return value.strip() if isinstance(value, str) else ''


def _finding(code, severity, module, record_id, title, field, detail, suggestion=None):
    suggestion = suggestion or (_SUGGESTIONS[code] % field if '%s' in _SUGGESTIONS.get(code, '')
                                else _SUGGESTIONS.get(code, '请人工核验并整改。'))
    return {'code': code, 'severity': severity, 'module': module, 'record_id': record_id,
            'title': title, 'field': field, 'detail': detail, 'suggestion': suggestion}


def _is_ended(data):
    status = data.get('状态') if isinstance(data, dict) else None
    return isinstance(status, str) and status in ENDED_STATUSES


def _has_status(module):
    return isinstance(module, str) and module in MODULES and '状态' in MODULES[module][1]


def _linked_open(row, rows, index):
    """True when an un-finished sub/rectification task is linked in either direction."""
    record_id, module = row['id'], row['module']
    for key in REFERENCES:
        target_id = row['data'].get(key)
        if not _text(target_id):
            continue
        target = index.get(target_id)
        if (not target or target.get('archived') or not _has_status(target.get('module'))
                or (REFERENCES[key] and target.get('module') != REFERENCES[key])):
            continue
        if not _is_ended(target.get('data') or {}):
            return True
    for other in rows:
        other_module = other['module']
        if other['id'] == record_id or not _has_status(other_module) or _is_ended(other['data']):
            continue
        for key in REFERENCES:
            if (REFERENCES[key] and REFERENCES[key] != module) or other['data'].get(key) != record_id:
                continue
            return True
    return False


def _record_findings(row, rows, index, today):
    module, data, record_id = row['module'], row['data'], row['id']
    title, fields, ended = _title(module, data), MODULES[module][1], _is_ended(data)
    status = data.get('状态', '')
    out = []
    for field in fields:
        if field in OWNER_FIELDS and not _text(data.get(field)):
            out.append(_finding('missing_owner', '中', module, record_id, title, field,
                                '负责人类字段为空，责任无法落实'))
    for field in KEY_DATES.get(module, ()):
        if not _text(data.get(field)):
            out.append(_finding('missing_date', '中', module, record_id, title, field,
                                '模块关键日期为空，无法安排到期或复核'))
    if status not in ('未开始', '目标设定'):
        for field in fields:
            if '证据' in field and not _text(data.get(field)):
                out.append(_finding('missing_evidence', '中', module, record_id, title, field,
                                    '证据类字段为空且记录已进入执行状态'))
    due = _parse_date(data.get('截止日期'))
    if not ended and due is not None and due < today and not _linked_open(row, rows, index):
        out.append(_finding('overdue_unhandled', '高', module, record_id, title, '截止日期',
                            '截止日期 %s 已过且未见未结的子任务或整改任务' % due.isoformat()))
    for field in REFERENCES:
        value = data.get(field)
        if not _text(value):
            continue
        target = index.get(value)
        if target is None:
            reason = '关联记录不存在'
        elif target.get('archived'):
            reason = '关联记录已归档'
        elif REFERENCES[field] and target.get('module') != REFERENCES[field]:
            reason = '关联记录模块不符'
        else:
            continue
        out.append(_finding('orphan_reference', '中', module, record_id, title, field,
                            '%s：%s' % (field, reason)))
    if module in ('access', 'risks') and not ended:
        review = _parse_date(data.get('复核日期'))
        if review is not None and review < today:
            out.append(_finding('review_overdue', '中', module, record_id, title, '复核日期',
                                '复核日期 %s 已过且记录未结束' % review.isoformat()))
    updated = _parse_date(row.get('updated'))
    if not ended and updated is not None and (today - updated).days > STALE_DAYS:
        out.append(_finding('stale_open', '低', module, record_id, title, 'updated',
                            '超过%d天未更新（最后更新 %s）' % (STALE_DAYS, updated.isoformat())))
    if module == 'systems':
        for field in SYSTEM_REQUIRED:
            if not _text(data.get(field)):
                out.append(_finding('system_no_recovery', '中', module, record_id, title, field,
                                    '系统台账缺少%s，无法评估恢复能力' % field))
    if module == 'reviews':
        for field in REVIEW_REQUIRED:
            if not _text(data.get(field)):
                out.append(_finding('review_missing_cycle', '低', module, record_id, title, field,
                                    '人员考核缺少%s' % field))
    return out


def _duplicate_findings(rows):
    groups = {}
    for row in rows:
        for field in UNIQUE_FIELDS.get(row['module'], ()):
            value = row['data'].get(field)
            if not _text(value):
                continue
            groups.setdefault((row['module'], field, value.strip()), []).append(row)
    out = []
    for (module, field, _), group in groups.items():
        if len(group) < 2:
            continue
        for row in group:
            out.append(_finding('duplicate_number', '中', module, row['id'], _title(module, row['data']),
                                field, '%s 与另外 %d 条有效记录取值重复' % (field, len(group) - 1)))
    return out


def _finding_sort(finding):
    return (_SEVERITY_RANK.get(finding['severity'], 9), finding['module'],
            finding['record_id'], finding['code'], finding['field'])


def scan(records, today=None) -> dict:
    """Analyse records and return findings plus their summary; writes nothing."""
    if not isinstance(records, list):
        raise ValueError('体检输入记录格式无效')
    if today is None:
        today = date.today()
    elif type(today) is not date:
        raise ValueError('体检扫描日期无效')
    rows, index = [], {}
    for row in records:
        if not isinstance(row, dict):
            raise ValueError('体检输入记录格式无效')
        record_id, module, data = row.get('id'), row.get('module'), row.get('data')
        if (not isinstance(record_id, str) or not record_id.strip()
                or not isinstance(module, str) or module not in MODULES
                or not isinstance(data, dict)):
            continue
        index[record_id] = row
        if not row.get('archived'):
            rows.append(row)
    findings = []
    for row in rows:
        findings.extend(_record_findings(row, rows, index, today))
    findings.extend(_duplicate_findings(rows))
    findings.sort(key=_finding_sort)
    return {'generated': datetime.now(timezone.utc).isoformat(), 'scanned': len(rows),
            'findings': findings, 'summary': summarize(findings)}


def summarize(findings) -> dict:
    """Count findings by severity, module and check code."""
    result = {'total': 0, 'high': 0, 'medium': 0, 'low': 0, 'by_module': {}, 'by_code': {}}
    for finding in findings or []:
        if not isinstance(finding, dict):
            continue
        result['total'] += 1
        severity = finding.get('severity')
        if severity == '高':
            result['high'] += 1
        elif severity == '中':
            result['medium'] += 1
        elif severity == '低':
            result['low'] += 1
        module, code = finding.get('module'), finding.get('code')
        if isinstance(module, str):
            result['by_module'][module] = result['by_module'].get(module, 0) + 1
        if isinstance(code, str):
            result['by_code'][code] = result['by_code'].get(code, 0) + 1
    return result


def finding_task_items(findings, limit=20) -> list[dict]:
    """Convert findings into store.create_actions items in the exact required order."""
    if not isinstance(findings, list):
        raise ValueError('体检输入记录格式无效')
    if type(limit) is not int or not 1 <= limit <= MAX_REMEDY_ITEMS:
        raise ValueError('体检整改项数量无效')
    due_date = (date.today() + timedelta(days=REMEDY_DAYS)).isoformat()
    items = []
    for finding in findings[:limit]:
        if not isinstance(finding, dict):
            continue
        title = _text(finding.get('title')) or _text(finding.get('code')) or '体检发现'
        field = _text(finding.get('field')) or _text(finding.get('code')) or '体检'
        next_step = (_text(finding.get('suggestion')) or _text(finding.get('detail'))
                     or '核对体检发现并完成整改')
        items.append({'name': '整改：' + title[:120] + '（' + field[:40] + '）',
                      'owner': '', 'due_date': due_date, 'next_step': next_step[:500],
                      'acceptance': '整改完成并留存证据'})
    return items
