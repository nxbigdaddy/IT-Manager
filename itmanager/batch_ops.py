"""Batch field/postpone/archive planning and filtered export: pure logic, no IO.

Every function here is deterministic and side-effect free: it performs no file,
database or network access, and never mutates the records it is given (each planned
data dict is a fresh shallow copy of the stored data with exactly one field replaced).
Every refusal carries a fixed message from SAFE_BATCH_ERRORS; untrusted values are
never interpolated into those messages.

Planned results are shaped for the existing batch contracts so the caller only has to
transport them:
  * plan_field_update / plan_postpone -> POST /records/batch
        {'updates': [{'id', 'version', 'data'}], 'request_id': <16-100 of [A-Za-z0-9_-]>}
  * plan_archive                      -> POST /records/batch-archive
        {'targets': [{'id', 'version'}], 'request_id': <16-100 of [A-Za-z0-9_-]>}
Both server calls are all-or-nothing, accept 1-200 items and reject duplicate ids, so
the caller submits only the planned changes/targets and keeps skipped records out.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from .schema import MODULES
from .store import NUMBERS

# Fixed refusals; never build these by formatting user input into them.
SAFE_BATCH_ERRORS = frozenset({
    '批量操作模块无效', '批量顺延天数无效', '批量顺延日期无效', '批量导出模块无效',
    '记录模块不符', '记录已归档', '记录ID无效', '记录重复选择',
    '该字段不允许批量修改', '值未发生变化', '值必须为文本', '值不能超过50000字符',
    '日期格式必须为YYYY-MM-DD', '数值必须为非负十进制文本',
    '完成任务前需先补齐完成证据与验收人', '已定稿考核只能改为待复核', '考核状态无效',
    '该模块没有可顺延的日期字段', '原日期无效，请先人工填写', '已结束记录不参与顺延',
    '顺延后的日期不能早于今天',
})

# Mirrors forms.REVIEW_STATUS / store._validate for 人员考核 status values.
REVIEW_STATUS = ('目标设定', '待评价', '待反馈', '待复核', '已定稿')
# Mirrors reminders.ENDED_STATUSES / health.ENDED_STATUSES.
ENDED_STATUSES = frozenset({'已完成', '已取消', '已关闭', '已定稿'})
# Date fields a whole selection may be postponed by, first match wins (exact names).
POSTPONE_FIELDS = ('截止日期', '到期日期', '维保到期日期', '复核日期', '日期')
# Field names excluded from the batch whitelist besides the display title and relations.
EXCLUDED_WORDS = ('证据', '评价', '反馈', '结论', '自评', '复盘', '根因')
EXCLUDED_FIELDS = frozenset({'受理时间', '恢复时间'})
RECORD_ID_HEADER = '记录ID'
MAX_VALUE_CHARS = 50000
MAX_FILENAME_CHARS = 120

_ISO_DATE = re.compile(r'\d{4}-\d{2}-\d{2}')
_DECIMAL = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)')
_UNSAFE_FILENAME_CHARS = re.compile(r'[^0-9A-Za-z_\u3400-\u4dbf\u4e00-\u9fff.\-]')
_FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')


def _rows(records):
    """Iterate records without holding or mutating the caller's container."""
    if isinstance(records, dict):
        return [records]
    if records is None or isinstance(records, (str, bytes)):
        return []
    try:
        return list(records)
    except TypeError:
        return []


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _data(record):
    data = record.get('data') if isinstance(record, dict) else None
    return dict(data) if isinstance(data, dict) else {}


def _oid(record):
    value = record.get('id') if isinstance(record, dict) else None
    return value if isinstance(value, str) and value else ''


def _parse_stored_date(value):
    """Return a date for a stored YYYY-MM-DD text, otherwise None."""
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_day(value):
    """Accept a date/datetime/ISO text as a calendar day; otherwise None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    return _parse_stored_date(str(value)[:10])


def _non_negative_number(value):
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        return False
    try:
        number = Decimal(value)
    except InvalidOperation:
        return False
    return number.is_finite() and number >= 0


def _is_ended(data):
    return isinstance(data.get('状态'), str) and data['状态'] in ENDED_STATUSES


def _base_refusal(record, module):
    """Shared per-record checks for every plan_* helper."""
    if not isinstance(record, dict) or record.get('module') != module:
        return '记录模块不符'
    if record.get('archived'):
        return '记录已归档'
    return None


def _change(record, data):
    return {'id': _oid(record) or record.get('id'), 'version': record.get('version'), 'data': data}


def _postpone_field(module):
    fields = MODULES[module][1]
    for name in POSTPONE_FIELDS:
        if name in fields:
            return name
    # Beyond the documented priority, any remaining date field keeps the module
    # postponable (e.g. recovery 演练日期); non-date timestamps never match.
    for name in fields:
        if name.endswith('日期'):
            return name
    return None


def _value_refusal(module, field, value, data):
    """Refusal for a single field value, already known to differ from the record."""
    if not isinstance(value, str):
        return '值必须为文本'
    if len(value) > MAX_VALUE_CHARS:
        return '值不能超过50000字符'
    if field.endswith('日期') and _parse_stored_date(value) is None:
        return '日期格式必须为YYYY-MM-DD'
    if field in NUMBERS and not _non_negative_number(value):
        return '数值必须为非负十进制文本'
    if module == 'tasks' and field == '状态' and value == '已完成':
        # store._validate: completion needs evidence and an acceptor in the same data.
        merged = dict(data, 状态=value)
        if not _text(merged.get('完成证据')) or not _text(merged.get('验收人')):
            return '完成任务前需先补齐完成证据与验收人'
    if module == 'reviews':
        # store.save_record / batch_update_records: an approved evaluation must be
        # re-opened on its own, so nothing else may travel in the same batch.
        if data.get('状态') == '已定稿':
            if field != '状态' or value != '待复核':
                return '已定稿考核只能改为待复核'
        elif field == '状态' and value not in REVIEW_STATUS:
            return '考核状态无效'
    return None


def _plan(changes, skipped, **summary):
    summary.update(total=len(changes) + len(skipped), changed=len(changes),
                   skipped=len(skipped))
    return {'changes': changes, 'skipped': skipped, 'summary': summary}


def batchable_fields(module):
    """Whitelisted fields for a whole-selection value change, in schema order.

    Excluded: the first display field (名称/姓名), every relation ending in 'ID',
    evidence/review/journal wording, and the incident timestamps.
    """
    if not isinstance(module, str) or module not in MODULES:
        raise ValueError('批量操作模块无效')
    result = []
    for index, name in enumerate(MODULES[module][1]):
        if index == 0 or name.endswith('ID') or name in EXCLUDED_FIELDS:
            continue
        if any(word in name for word in EXCLUDED_WORDS):
            continue
        result.append(name)
    return result


def plan_field_update(records, module, field, value):
    """Plan one field value for the selected records; nothing is written.

    Each change carries the complete new data (shallow copy of the stored data with
    the field replaced) plus the record's own version for compare-and-set.
    """
    allowed = batchable_fields(module)
    field_allowed = isinstance(field, str) and field in allowed
    changes, skipped = [], []
    for record in _rows(records):
        data = _data(record)
        refusal = _base_refusal(record, module)
        if refusal is None and not field_allowed:
            refusal = '该字段不允许批量修改'
        if refusal is None and data.get(field, '') == value:
            refusal = '值未发生变化'
        if refusal is None:
            refusal = _value_refusal(module, field, value, data)
        if refusal is None:
            changes.append(_change(record, dict(data, **{field: value})))
        else:
            skipped.append({'id': _oid(record) or record.get('id'), 'reason': refusal})
    return _plan(changes, skipped, module=module, field=field, value=value)


def plan_postpone(records, module, days, new_date=None, today=None):
    """Plan a date shift for the selection; nothing is written.

    ``days`` must be an int within 1-3650 and is always required. ``new_date`` pins
    one fixed YYYY-MM-DD instead of per-record arithmetic; ``today`` is injectable
    for tests and defaults to date.today().
    """
    if not isinstance(module, str) or module not in MODULES:
        raise ValueError('批量操作模块无效')
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError('批量顺延天数无效')
    fixed = None
    if new_date is not None:
        fixed = _parse_day(new_date)
        if fixed is None:
            raise ValueError('批量顺延日期无效')
    reference = _parse_day(today) if today is not None else date.today()
    if reference is None:
        raise ValueError('批量顺延日期无效')
    field = _postpone_field(module)
    changes, skipped = [], []
    for record in _rows(records):
        data = _data(record)
        refusal = _base_refusal(record, module)
        if refusal is None and field is None:
            refusal = '该模块没有可顺延的日期字段'
        if refusal is None and _is_ended(data):
            refusal = '已结束记录不参与顺延'
        shifted = None
        if refusal is None:
            current = _parse_stored_date(data.get(field))
            if current is None:
                refusal = '原日期无效，请先人工填写'
            else:
                shifted = fixed or current + timedelta(days=days)
                if shifted < reference:
                    refusal = '顺延后的日期不能早于今天'
        if refusal is None:
            changes.append(_change(record, dict(data, **{field: shifted.isoformat()})))
        else:
            skipped.append({'id': _oid(record) or record.get('id'), 'reason': refusal})
    return _plan(changes, skipped, module=module, field=field, days=days,
                 new_date=fixed.isoformat() if fixed else None, today=reference.isoformat())


def plan_archive(records):
    """Plan archiving for the selection: targets for /records/batch-archive.

    Already archived records are skipped; duplicates and records without a usable id
    are skipped too so the all-or-nothing server call cannot be rejected as a whole.
    """
    targets, skipped, seen = [], [], set()
    for record in _rows(records):
        oid = _oid(record)
        if isinstance(record, dict) and record.get('archived'):
            skipped.append({'id': oid or record.get('id'), 'reason': '记录已归档'})
        elif not oid:
            skipped.append({'id': None, 'reason': '记录ID无效'})
        elif oid in seen:
            skipped.append({'id': oid, 'reason': '记录重复选择'})
        else:
            seen.add(oid)
            targets.append({'id': oid, 'version': record.get('version')})
    return {'targets': targets, 'skipped': skipped}


def export_rows(records, module, columns=None):
    """Export the caller's already-filtered records as string cells.

    The first column is 记录ID, then every schema field of the module (or the subset
    named by ``columns``, still in schema order). Missing/None values become ''. Text
    keeps commas and line breaks; CSV quoting is left to to_csv().
    """
    if not isinstance(module, str) or module not in MODULES:
        raise ValueError('批量导出模块无效')
    fields = list(MODULES[module][1])
    if columns is not None:
        wanted = {name for name in columns if isinstance(name, str)}
        fields = [name for name in fields if name in wanted]
    headers = [RECORD_ID_HEADER] + fields
    rows = []
    for record in _rows(records):
        if not isinstance(record, dict):
            continue
        owner = record.get('module')
        if isinstance(owner, str) and owner and owner != module:
            continue
        data = record.get('data') if isinstance(record.get('data'), dict) else {}
        cells = ['' if record.get('id') is None else str(record['id'])]
        cells.extend('' if data.get(name) is None else str(data.get(name)) for name in fields)
        rows.append(cells)
    return {'headers': headers, 'rows': rows, 'module': module,
            'generated': datetime.now(timezone.utc).isoformat()}


def _csv_cell(value):
    text = '' if value is None else str(value)
    if text.startswith(_FORMULA_PREFIXES):
        # Excel/LibreOffice would treat these as formulas; force text instead.
        text = "'" + text
    if any(char in text for char in (',', '"', '\n', '\r')):
        text = '"' + text.replace('"', '""') + '"'
    return text


def to_csv(headers, rows):
    """RFC4180 text: comma separated, doubled quotes, CRLF endings, header first."""
    lines = [list(headers or [])]
    lines.extend(list(row or []) for row in (rows or []))
    if len(lines) == 1 and not lines[0]:
        return ''
    return ''.join(','.join(_csv_cell(cell) for cell in line) + '\r\n' for line in lines)


def _stamp(when):
    day = _parse_day(when)
    text = day.isoformat() if day else ('' if when is None else str(when)[:10])
    return _UNSAFE_FILENAME_CHARS.sub('', text).strip('._-')


def safe_filename(module, when):
    """A short, path-safe export name such as 'IT台账_任务管理_2026-10-10.csv'."""
    title = MODULES[module][0] if isinstance(module, str) and module in MODULES else module
    body = _UNSAFE_FILENAME_CHARS.sub('', title if isinstance(title, str) else '').strip('._-')
    body = body or '导出'
    stamp = _stamp(when)
    prefix, suffix = 'IT台账_', '.csv'
    middle = f'{body}_{stamp}' if stamp else body
    name = f'{prefix}{middle}{suffix}'
    if len(name) > MAX_FILENAME_CHARS:
        # Shorten the title first so the date stamp survives; the final guard keeps the
        # extension only for a pathologically long stamp.
        room = MAX_FILENAME_CHARS - len(prefix) - len(suffix) - (len(stamp) + 1 if stamp else 0)
        middle = f'{body[:max(1, room)]}_{stamp}' if stamp else body[:max(1, room)]
        name = f'{prefix}{middle}{suffix}'
        if len(name) > MAX_FILENAME_CHARS:
            name = name[:MAX_FILENAME_CHARS - len(suffix)] + suffix
    return name
