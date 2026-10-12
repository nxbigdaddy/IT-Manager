"""Backup planning arithmetic and recovery-drill records: pure logic, no I/O.

Design rules
------------
* No file, database, network or clock access.  Every function receives the
  current moment as an argument (``after`` / ``now`` / ``today``), so importing
  this module has no side effects and every branch is testable without patching.
* Every rejection is a ``ValueError`` whose message is a member of
  :data:`SAFE_BACKUP_ERRORS` (fixed, non-sensitive Chinese text), so callers may
  show the message directly in the GUI or return it from the server without
  leaking internal details.
* Planned slots are computed with ``zoneinfo.ZoneInfo`` in the plan timezone and
  returned as ISO-8601 strings that carry a UTC offset.  ``datetime.fromisoformat``
  on such a string yields an aware datetime, so the existing scheduler pattern
  (``datetime.now(timezone.utc) < due``) keeps working across offsets.  DST is
  never fatal: a nonexistent local time is mapped forward by the zone shift, and
  an ambiguous local time uses the first occurrence (``fold=0``).
* Nothing here deletes anything: :func:`retention_plan` only *advises* which
  backup files are kept and which may be removed.

Recovery drill field mapping (real ``itmanager.schema`` recovery fields)
-----------------------------------------------------------------------
The recovery module fields are 名称 / 系统ID / 负责人 / 备份范围 / 频率及保留 /
隔离策略 / RTO小时 / RPO小时 / 演练日期 / 实际耗时 / 实际数据缺口 / 结论 /
证据 / 整改任务ID.  :func:`drill_record` maps its arguments onto exactly those
names and never invents new ones:

* ``plan_scope``  -> ``备份范围``
* ``operator``    -> ``负责人``
* ``drilled_on``  -> ``演练日期``
* ``result``      -> ``结论``
* ``findings``    -> ``实际数据缺口`` (the closest real field for drill findings)
* ``evidence``    -> ``证据``
* ``名称`` is synthesized as the display title, because the recovery form's
  first field is the title and no title argument is accepted.

Unknown/dropped values: ``next_drill`` is validated as a real ``YYYY-MM-DD``
date but then **dropped**, because the recovery schema has no "next drill"
field and writing it into an unrelated field (比如 ``频率及保留`` 或 ``证据``)
would corrupt real data.  Callers that need to store the next drill date must
keep it outside the recovery record (for example in a task or reminder).  The
remaining recovery fields (系统ID / 频率及保留 / 隔离策略 / RTO小时 / RPO小时 /
实际耗时 / 整改任务ID) have no argument either and are simply absent from the
returned dictionary; a caller may merge additional real fields into the result
before saving.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

__all__ = [
    'SAFE_BACKUP_ERRORS',
    'MAX_MISSED_SLOTS',
    'PLAN_FIELDS',
    'FREQUENCIES',
    'DRILL_RESULTS',
    'validate_plan',
    'next_run',
    'missed_slots',
    'retention_plan',
    'drill_record',
    'drill_reminder',
    'backup_health',
]

#: Fixed, non-sensitive messages.  Every ValueError raised by this module uses
#: one of these strings, so callers can safely display them verbatim.
SAFE_BACKUP_ERRORS = frozenset({
    '备份计划参数无效',
    '备份频率无效',
    '备份时区无效',
    '备份保留数量无效',
    '备份文件列表无效',
    '备份时间格式无效',
    '恢复演练字段无效',
    '恢复演练日期无效',
})

#: ``missed_slots`` never returns more than this many slots (truncated).
MAX_MISSED_SLOTS = 50

#: Accepted keys of a plan dictionary; anything else is rejected.
PLAN_FIELDS = ('enabled', 'frequency', 'timezone', 'hour', 'minute', 'weekday',
               'day', 'keep', 'scope')

FREQUENCIES = ('daily', 'weekly', 'monthly')
DRILL_RESULTS = ('通过', '部分通过', '失败')

#: Hard bounds for a plan.  ``day`` stops at 28 on purpose: a 29-31 monthly day
#: is ambiguous in short months, and this module never clamps to month end.
_KEEP_RANGE = (1, 365)
_MAX_AGE_RANGE = (1, 3650)
_WARN_DAYS_RANGE = (1, 3650)
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_SEARCH_DAYS = 370  # covers any weekly/monthly slot with margin


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _zone(name):
    """Resolve a timezone name, mapping any failure onto the safe message.

    Callers pass ``'UTC'`` themselves when the plan omits ``timezone``; an
    explicitly present but unusable value (including ``None``) is rejected.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError('备份时区无效')
    try:
        return ZoneInfo(name)
    except Exception:  # ZoneInfoNotFoundError, ValueError, OSError, ...
        raise ValueError('备份时区无效') from None


def _aware(value):
    """Coerce ``datetime``/ISO string into an aware datetime.

    Naive datetimes are a plan/parameter error; unparsable or offset-less
    strings are a time-format error.
    """
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError('备份计划参数无效')
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(('Z', 'z')):
            text = text[:-1] + '+00:00'
        try:
            parsed = datetime.fromisoformat(text)
        except Exception:
            raise ValueError('备份时间格式无效') from None
        if parsed.utcoffset() is None:
            raise ValueError('备份时间格式无效')
        return parsed
    raise ValueError('备份计划参数无效')


def _created_stamp(value, zone):
    """Parse a backup file ``created`` value; ``None`` when unusable.

    ISO date strings ("2024-01-10") and ISO datetimes are accepted.  Naive
    values (no offset) are interpreted in ``zone``.
    """
    if isinstance(value, datetime):
        return value if value.utcoffset() is not None else value.replace(tzinfo=zone)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=zone)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith(('Z', 'z')):
            text = text[:-1] + '+00:00'
        try:
            parsed = datetime.fromisoformat(text)
        except Exception:
            try:
                day = date.fromisoformat(text)
            except Exception:
                return None
            return datetime(day.year, day.month, day.day, tzinfo=zone)
        if parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=zone)
        return parsed
    return None


def _int_in_range(value, low, high):
    return type(value) is int and low <= value <= high


def _strict_date(value):
    """Return a ``date`` for a strict ``YYYY-MM-DD`` string, else raise."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value.strip()):
        raise ValueError('恢复演练日期无效')
    try:
        return date.fromisoformat(value.strip())
    except Exception:
        raise ValueError('恢复演练日期无效') from None


def _optional_text(value, message='恢复演练字段无效'):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError(message)
    return value


def _file_entry(item, zone):
    """Normalize one backup file dict; raises for non-dict elements."""
    if not isinstance(item, dict):
        raise ValueError('备份文件列表无效')
    name = item.get('name')
    if not isinstance(name, str):
        name = ''
    size = item.get('size')
    if type(size) is not int or size < 0:
        size = 0
    created = item.get('created')
    if isinstance(created, str):
        created_out = created
    elif isinstance(created, (datetime, date)):
        created_out = created.isoformat()
    else:
        created_out = ''
    return {
        'name': name,
        'size': size,
        'created': created_out,
        'stamp': _created_stamp(created, zone),
    }


# --------------------------------------------------------------------------- #
# plan validation
# --------------------------------------------------------------------------- #
def validate_plan(plan):
    """Validate a backup plan; return ``None`` or raise a safe ``ValueError``.

    ``plan`` is ``{'enabled': bool, 'frequency': 'daily'|'weekly'|'monthly',
    'timezone': str, 'hour': 0-23, 'minute': 0-59, 'weekday': 0-6,
    'day': 1-28, 'keep': 1-365, 'scope': str}``.  Only ``frequency`` is
    mandatory; omitted fields fall back to the same defaults used by
    :func:`next_run` (UTC, 00:00, weekday 0, day 1, keep 7, enabled True).
    A ``weekly`` plan must state ``weekday`` and a ``monthly`` plan must state
    ``day`` explicitly, so a typo cannot silently change the schedule.
    """
    if not isinstance(plan, dict):
        raise ValueError('备份计划参数无效')
    if set(plan) - set(PLAN_FIELDS):
        raise ValueError('备份计划参数无效')
    frequency = plan.get('frequency')
    if frequency is None:
        raise ValueError('备份频率无效')
    if frequency not in FREQUENCIES:
        raise ValueError('备份频率无效')
    if 'enabled' in plan and type(plan['enabled']) is not bool:
        raise ValueError('备份计划参数无效')
    if 'timezone' in plan:
        _zone(plan['timezone'])
    for key, low, high in (('hour', 0, 23), ('minute', 0, 59),
                           ('weekday', 0, 6), ('day', 1, 28)):
        if key in plan and not _int_in_range(plan[key], low, high):
            raise ValueError('备份计划参数无效')
    if 'keep' in plan and not _int_in_range(plan['keep'], *_KEEP_RANGE):
        raise ValueError('备份保留数量无效')
    if 'scope' in plan and not isinstance(plan['scope'], str):
        raise ValueError('备份计划参数无效')
    if frequency == 'weekly' and 'weekday' not in plan:
        raise ValueError('备份计划参数无效')
    if frequency == 'monthly' and 'day' not in plan:
        raise ValueError('备份计划参数无效')
    return None


# --------------------------------------------------------------------------- #
# scheduling
# --------------------------------------------------------------------------- #
def next_run(plan, after):
    """Next planned backup instant strictly after ``after``, as an ISO string.

    ``after`` must be an aware ``datetime`` (an ISO string with an offset is
    also accepted).  The scan starts on the local date of ``after`` in the plan
    timezone and returns the slot in that same timezone, so the offset shown in
    the result is the real offset in effect for that slot.
    """
    validate_plan(plan)
    moment = _aware(after)
    zone = _zone(plan.get('timezone', 'UTC'))
    frequency = plan['frequency']
    hour, minute = plan.get('hour', 0), plan.get('minute', 0)
    weekday, day = plan.get('weekday', 0), plan.get('day', 1)
    after_utc = moment.astimezone(timezone.utc)
    local_date = after_utc.astimezone(zone).date()
    for offset in range(_SEARCH_DAYS):
        current = local_date + timedelta(days=offset)
        if frequency == 'weekly' and current.weekday() != weekday:
            continue
        if frequency == 'monthly' and current.day != day:
            continue
        local = datetime(current.year, current.month, current.day, hour, minute,
                         tzinfo=zone, fold=0)
        instant = local.astimezone(timezone.utc)
        if instant > after_utc:
            return instant.astimezone(zone).isoformat()
    raise ValueError('备份计划参数无效')


def missed_slots(plan, last_run, now):
    """Scheduled instants missed between ``last_run`` and ``now`` (ISO strings).

    Answers "服务停机期间错过了几次备份".  ``last_run`` (aware datetime or ISO
    string) is the last successful run; ``None`` means "never ran", which
    yields an empty list rather than an unbounded history.  At most
    :data:`MAX_MISSED_SLOTS` slots are returned; the list is truncated (not
    summarised) once that many were missed.
    """
    validate_plan(plan)
    if last_run is None:
        return []
    start = _aware(last_run)
    end = _aware(now)
    if end <= start:
        return []
    slots = []
    cursor = start
    while len(slots) < MAX_MISSED_SLOTS:
        candidate_iso = next_run(plan, cursor)
        candidate = _aware(candidate_iso)
        if candidate > end:
            break
        slots.append(candidate_iso)
        cursor = candidate
    return slots


# --------------------------------------------------------------------------- #
# retention
# --------------------------------------------------------------------------- #
def retention_plan(files, keep):
    """Advise which backups to keep and which old ones may be deleted.

    ``files`` is ``[{'name', 'size', 'created'(ISO)}]`` and ``keep`` is the
    number of newest backups to retain (1-365).  Returns
    ``{'keep': [...], 'delete': [...], 'total_size': int, 'keep_size': int,
    'delete_size': int}`` where each list holds normalized
    ``{'name', 'size', 'created'}`` dictionaries.  ``keep`` is the newest-first
    ranking (unparsable/missing ``created`` sorts last, so such files are only
    kept once every dated backup is kept); ``delete`` is ordered for the
    operator: unusable-``created`` files first, then the remaining files
    oldest-first.  **Nothing is deleted here**: the caller decides, this only
    recommends.
    """
    if not _int_in_range(keep, *_KEEP_RANGE):
        raise ValueError('备份保留数量无效')
    if not isinstance(files, list):
        raise ValueError('备份文件列表无效')
    entries = []
    for index, item in enumerate(files):
        entry = _file_entry(item, timezone.utc)
        entry['_order'] = index
        entries.append(entry)

    def sort_key(entry):
        stamp = entry['stamp']
        if stamp is None:
            return (1, 0.0, entry['_order'])
        return (0, -stamp.timestamp(), entry['_order'])

    ordered = sorted(entries, key=sort_key)
    kept, deleted = ordered[:keep], ordered[keep:]
    # "优先删除": unparsable timestamps first, then the oldest dated backups.
    deleted = sorted(deleted, key=lambda entry: (
        0 if entry['stamp'] is None else 1,
        0.0 if entry['stamp'] is None else entry['stamp'].timestamp(),
        entry['_order'],
    ))

    def public(entry):
        return {'name': entry['name'], 'size': entry['size'], 'created': entry['created']}

    return {
        'keep': [public(entry) for entry in kept],
        'delete': [public(entry) for entry in deleted],
        'total_size': sum(entry['size'] for entry in entries),
        'keep_size': sum(entry['size'] for entry in kept),
        'delete_size': sum(entry['size'] for entry in deleted),
    }


# --------------------------------------------------------------------------- #
# recovery drills
# --------------------------------------------------------------------------- #
def drill_record(plan_scope, drilled_on, result, findings='', operator='',
                 evidence='', next_drill=None):
    """Build a recovery-module record dictionary for one backup drill.

    Field names are the real ``itmanager.schema`` recovery fields; see the
    module docstring for the exact mapping and for the values that are dropped
    (``next_drill`` is validated and then discarded, because recovery has no
    next-drill field).  ``drilled_on`` must be ``YYYY-MM-DD`` and ``result``
    must be one of :data:`DRILL_RESULTS`.
    """
    _strict_date(drilled_on)
    if result not in DRILL_RESULTS:
        raise ValueError('恢复演练字段无效')
    _optional_text(plan_scope)
    findings = _optional_text(findings)
    operator = _optional_text(operator)
    evidence = _optional_text(evidence)
    if next_drill is not None:
        _strict_date(next_drill)  # validated, then intentionally dropped
    title = '备份恢复演练：' + plan_scope if plan_scope else '备份恢复演练'
    return {
        '名称': title,
        '负责人': operator,
        '备份范围': plan_scope,
        '演练日期': drilled_on.strip(),
        '实际数据缺口': findings,
        '结论': result,
        '证据': evidence,
    }


def drill_reminder(drill_records, today, warn_days=30):
    """Turn recovery records into "演练到期/即将到期" reminders.

    ``drill_records`` accepts raw recovery ``data`` dictionaries or stored
    records (``{'id', 'module', 'data': {...}}``); a record without the real
    ``演练日期`` field (or with an unparsable date) is skipped, never guessed.
    ``today`` must be a ``date``.  ``warn_days`` is the maximum allowed gap
    since the last drill: ``days_left = warn_days - (today - 演练日期).days``.

    Severity: ``days_left < 0`` → 高 (already overdue), ``days_left == 0`` → 中
    (due exactly now), ``0 < days_left <= 3`` → 中, ``3 < days_left <= 7`` → 低;
    anything further away is not yet a reminder and is omitted.  Results are
    sorted by urgency and each item is
    ``{'record_id','title','date','days_left','severity','message'}``.
    """
    if isinstance(today, datetime):
        base = today.date()
    elif isinstance(today, date):
        base = today
    else:
        raise ValueError('恢复演练日期无效')
    if not _int_in_range(warn_days, *_WARN_DAYS_RANGE):
        raise ValueError('恢复演练字段无效')
    if not isinstance(drill_records, list):
        raise ValueError('恢复演练字段无效')

    reminders = []
    for item in drill_records:
        if not isinstance(item, dict):
            continue
        data = item.get('data')
        if not isinstance(data, dict):
            data = item
        raw_date = data.get('演练日期')
        measured = None
        if isinstance(raw_date, datetime):
            measured = raw_date.date()
        elif isinstance(raw_date, date):
            measured = raw_date
        elif isinstance(raw_date, str):
            try:
                measured = _strict_date(raw_date)
            except ValueError:
                measured = None
        if measured is None:
            continue  # no usable date -> skip, do not guess
        record_id = item.get('record_id', item.get('id', ''))
        if record_id is None:
            record_id = ''
        title = data.get('名称')
        if not isinstance(title, str):
            title = ''
        days_since = (base - measured).days
        days_left = warn_days - days_since
        if days_left < 0:
            severity = '高'
            message = '距上次演练已超过 %d 天（已 %d 天），请尽快安排恢复演练' % (warn_days, days_since)
        elif days_left == 0:
            severity = '中'
            message = '恢复演练已到期（距上次演练 %d 天），请安排演练' % days_since
        elif days_left <= 3:
            severity = '中'
            message = '距上次演练 %d 天，演练即将到期（剩余 %d 天）' % (days_since, days_left)
        elif days_left <= 7:
            severity = '低'
            message = '距上次演练 %d 天，请提前安排演练（剩余 %d 天）' % (days_since, days_left)
        else:
            continue
        reminders.append({'record_id': record_id, 'title': title,
                          'date': measured.isoformat(), 'days_left': days_left,
                          'severity': severity, 'message': message})
    reminders.sort(key=lambda item: (item['days_left'], str(item['record_id'])))
    return reminders


# --------------------------------------------------------------------------- #
# health summary
# --------------------------------------------------------------------------- #
def backup_health(files, plan, now, max_age_days=7):
    """Summarize backup freshness and missed slots for the workbench/体检.

    Returns ``{'ok': bool, 'reasons': [str], 'newest': name|None,
    'age_days': int|None, 'missed': int}``.  ``ok`` is False when there is no
    usable backup file, when the newest backup is older than ``max_age_days``,
    or when the plan is enabled and slots were missed since that newest backup.
    ``missed`` counts planned instants between the newest backup moment and
    ``now`` (date-only ``created`` values are anchored to 00:00 in the plan
    timezone, a deliberately conservative reading).
    """
    validate_plan(plan)
    moment = _aware(now)
    if not _int_in_range(max_age_days, *_MAX_AGE_RANGE):
        raise ValueError('备份计划参数无效')
    if not isinstance(files, list):
        raise ValueError('备份文件列表无效')
    zone = _zone(plan.get('timezone', 'UTC'))

    newest_name = None
    newest_stamp = None
    for item in files:
        if not isinstance(item, dict):
            raise ValueError('备份文件列表无效')
        stamp = _created_stamp(item.get('created'), zone)
        if stamp is None:
            continue
        if newest_stamp is None or stamp > newest_stamp:
            newest_stamp = stamp
            name = item.get('name')
            newest_name = name if isinstance(name, str) else ''

    reasons = []
    age_days = None
    if not files:
        reasons.append('没有可用的备份文件')
    elif newest_stamp is None:
        reasons.append('没有可用的备份文件（备份时间无效）')
    else:
        age_days = (moment.astimezone(zone).date() - newest_stamp.astimezone(zone).date()).days
        if age_days > max_age_days:
            reasons.append('最新备份已超过 %d 天（%d 天）' % (max_age_days, age_days))

    missed = 0
    if newest_stamp is not None:
        missed = len(missed_slots(plan, newest_stamp, moment))
    if plan.get('enabled', True) and missed:
        reasons.append('备份计划已启用但错过了 %d 个备份时刻' % missed)

    return {'ok': not reasons, 'reasons': reasons, 'newest': newest_name,
            'age_days': age_days, 'missed': missed}
