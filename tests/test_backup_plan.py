"""Tests for the pure backup-planning layer (no I/O, network or real clock).

Every "now"/"today" is supplied by the test; nothing here reads the system
clock, the database or the filesystem.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from itmanager.backup_plan import (
    DRILL_RESULTS,
    MAX_MISSED_SLOTS,
    SAFE_BACKUP_ERRORS,
    backup_health,
    drill_record,
    drill_reminder,
    missed_slots,
    next_run,
    retention_plan,
    validate_plan,
)
from itmanager.schema import MODULES

CST = timezone(timedelta(hours=8))
UTC = timezone.utc
RECOVERY_FIELDS = set(MODULES['recovery'][1])


def dt(text):
    return datetime.fromisoformat(text)


DAILY_SH = {'enabled': True, 'frequency': 'daily', 'timezone': 'Asia/Shanghai',
            'hour': 2, 'minute': 30, 'keep': 7, 'scope': '核心库'}


# --------------------------------------------------------------------------- #
# validate_plan
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize('plan', [
    {'frequency': 'daily'},
    DAILY_SH,
    {'frequency': 'weekly', 'timezone': 'UTC', 'hour': 0, 'minute': 0, 'weekday': 6},
    {'frequency': 'monthly', 'timezone': 'Asia/Shanghai', 'day': 28, 'enabled': False},
])
def test_validate_plan_accepts_valid_plans(plan):
    assert validate_plan(plan) is None


@pytest.mark.parametrize('plan', [
    None, [], 'daily', 7, ('daily',),
])
def test_validate_plan_rejects_non_dict(plan):
    with pytest.raises(ValueError) as excinfo:
        validate_plan(plan)
    assert str(excinfo.value) == '备份计划参数无效'


def test_validate_plan_rejects_unknown_fields():
    plan = dict(DAILY_SH, type='daily')  # legacy notifications key is not a plan field
    with pytest.raises(ValueError) as excinfo:
        validate_plan(plan)
    assert str(excinfo.value) == '备份计划参数无效'


@pytest.mark.parametrize('plan', [
    {},
    {'frequency': None},
    {'frequency': 'yearly'},
    {'frequency': 'DAILY'},
    {'frequency': 1},
])
def test_validate_plan_rejects_bad_frequency(plan):
    with pytest.raises(ValueError) as excinfo:
        validate_plan(plan)
    assert str(excinfo.value) == '备份频率无效'


def test_validate_plan_weekly_requires_weekday():
    with pytest.raises(ValueError) as excinfo:
        validate_plan({'frequency': 'weekly', 'hour': 3})
    assert str(excinfo.value) == '备份计划参数无效'
    # weekday 0 is a real value and must be accepted once stated explicitly.
    assert validate_plan({'frequency': 'weekly', 'weekday': 0}) is None


def test_validate_plan_monthly_requires_day():
    with pytest.raises(ValueError) as excinfo:
        validate_plan({'frequency': 'monthly', 'hour': 3})
    assert str(excinfo.value) == '备份计划参数无效'
    assert validate_plan({'frequency': 'monthly', 'day': 1}) is None


@pytest.mark.parametrize('plan', [
    {'frequency': 'monthly', 'day': 29},
    {'frequency': 'monthly', 'day': 31},
    {'frequency': 'monthly', 'day': 0},
    {'frequency': 'daily', 'day': 30},
])
def test_validate_plan_rejects_day_above_28(plan):
    with pytest.raises(ValueError) as excinfo:
        validate_plan(plan)
    assert str(excinfo.value) == '备份计划参数无效'


@pytest.mark.parametrize('zone', ['Mars/Olympus', 'Not/AZone', '', 42, None, 'UTC+8'])
def test_validate_plan_rejects_bad_timezone(zone):
    with pytest.raises(ValueError) as excinfo:
        validate_plan({'frequency': 'daily', 'timezone': zone})
    assert str(excinfo.value) == '备份时区无效'


def test_validate_plan_accepts_real_timezones():
    for zone in ('UTC', 'Asia/Shanghai', 'America/New_York', 'Europe/Berlin'):
        assert validate_plan({'frequency': 'daily', 'timezone': zone}) is None


@pytest.mark.parametrize('patch', [
    {'hour': 24}, {'hour': -1}, {'hour': '9'}, {'hour': True},
    {'minute': 60}, {'minute': -1}, {'minute': 0.5},
    {'weekday': 7}, {'weekday': -1}, {'weekday': 1.0},
    {'enabled': 'true'}, {'enabled': 1}, {'enabled': None},
    {'scope': 5}, {'scope': None},
])
def test_validate_plan_rejects_out_of_range_or_wrong_typed_fields(patch):
    plan = dict(DAILY_SH)
    plan.update(patch)
    if 'weekday' in patch:
        plan['frequency'] = 'weekly'
    with pytest.raises(ValueError) as excinfo:
        validate_plan(plan)
    assert str(excinfo.value) in ('备份计划参数无效', '备份保留数量无效')


@pytest.mark.parametrize('keep', [0, -1, 366, 1000, 2.0, '7', True, None])
def test_validate_plan_rejects_keep_out_of_range(keep):
    with pytest.raises(ValueError) as excinfo:
        validate_plan(dict(DAILY_SH, keep=keep))
    assert str(excinfo.value) == '备份保留数量无效'


# --------------------------------------------------------------------------- #
# next_run
# --------------------------------------------------------------------------- #
def test_next_run_daily_is_strictly_after():
    plan = {'frequency': 'daily', 'timezone': 'Asia/Shanghai', 'hour': 9, 'minute': 0}
    assert next_run(plan, dt('2024-01-01T00:00:00+08:00')) == '2024-01-01T09:00:00+08:00'
    assert next_run(plan, dt('2024-01-01T08:59:59+08:00')) == '2024-01-01T09:00:00+08:00'
    # Exactly on the slot: the *next* slot, never the same instant.
    assert next_run(plan, dt('2024-01-01T09:00:00+08:00')) == '2024-01-02T09:00:00+08:00'
    assert next_run(plan, dt('2024-01-01T10:00:00+08:00')) == '2024-01-02T09:00:00+08:00'


def test_next_run_uses_plan_timezone_offset_not_utc():
    plan = {'frequency': 'daily', 'timezone': 'Asia/Shanghai', 'hour': 9, 'minute': 0}
    assert next_run(plan, dt('2024-01-01T00:00:00+00:00')) == '2024-01-01T09:00:00+08:00'
    plan_utc = {'frequency': 'daily', 'timezone': 'UTC', 'hour': 9, 'minute': 0}
    assert next_run(plan_utc, dt('2024-01-01T00:00:00+00:00')) == '2024-01-01T09:00:00+00:00'


def test_next_run_matches_the_same_instant_in_utc():
    plan = {'frequency': 'daily', 'timezone': 'America/New_York', 'hour': 8, 'minute': 0}
    result = next_run(plan, dt('2024-07-01T00:00:00+00:00'))
    assert result == '2024-07-01T08:00:00-04:00'
    assert dt(result).astimezone(UTC) == dt('2024-07-01T12:00:00+00:00')
    winter = next_run(plan, dt('2024-01-01T00:00:00+00:00'))
    assert winter == '2024-01-01T08:00:00-05:00'
    assert dt(winter).astimezone(UTC) == dt('2024-01-01T13:00:00+00:00')


def test_next_run_weekly_uses_weekday():
    plan = {'frequency': 'weekly', 'timezone': 'Asia/Shanghai', 'hour': 9,
            'minute': 30, 'weekday': 2}  # Wednesday
    assert next_run(plan, dt('2024-01-01T00:00:00+08:00')) == '2024-01-03T09:30:00+08:00'
    assert next_run(plan, dt('2024-01-03T09:30:00+08:00')) == '2024-01-10T09:30:00+08:00'
    assert next_run(plan, dt('2024-01-03T09:30:01+08:00')) == '2024-01-10T09:30:00+08:00'


def test_next_run_monthly_crosses_months_and_year():
    plan = {'frequency': 'monthly', 'timezone': 'Asia/Shanghai', 'hour': 9, 'day': 28}
    assert next_run(plan, dt('2024-01-28T09:00:00+08:00')) == '2024-02-28T09:00:00+08:00'
    assert next_run(plan, dt('2024-02-28T09:00:00+08:00')) == '2024-03-28T09:00:00+08:00'
    plan_5 = dict(plan, day=5)
    assert next_run(plan_5, dt('2024-12-05T09:00:00+08:00')) == '2025-01-05T09:00:00+08:00'


def test_next_run_daily_crosses_year_boundary():
    plan = {'frequency': 'daily', 'timezone': 'Asia/Shanghai', 'hour': 0, 'minute': 0}
    assert next_run(plan, dt('2024-12-31T23:00:00+08:00')) == '2025-01-01T00:00:00+08:00'
    assert next_run(plan, dt('2024-02-28T23:30:00+08:00')) == '2024-02-29T00:00:00+08:00'  # leap day


def test_next_run_survives_dst_gap():
    plan = {'frequency': 'daily', 'timezone': 'America/New_York', 'hour': 2, 'minute': 30}
    result = next_run(plan, dt('2024-03-10T00:00:00-05:00'))
    assert result == '2024-03-10T03:30:00-04:00'
    assert dt(result) > dt('2024-03-10T00:00:00-05:00')


def test_next_run_survives_dst_overlap_using_first_occurrence():
    plan = {'frequency': 'daily', 'timezone': 'America/New_York', 'hour': 1, 'minute': 30}
    result = next_run(plan, dt('2024-11-03T00:00:00-04:00'))
    assert result == '2024-11-03T01:30:00-04:00'
    assert dt(result).astimezone(UTC) == dt('2024-11-03T05:30:00+00:00')


def test_next_run_accepts_iso_string_after():
    plan = {'frequency': 'daily', 'timezone': 'UTC', 'hour': 6}
    assert next_run(plan, '2024-01-01T00:00:00+00:00') == '2024-01-01T06:00:00+00:00'
    assert next_run(plan, '2024-01-01T06:00:00+00:00') == '2024-01-02T06:00:00+00:00'
    assert next_run(plan, '2024-01-01T06:00:00Z') == '2024-01-02T06:00:00+00:00'


@pytest.mark.parametrize('after', [
    datetime(2024, 1, 1, 0, 0, 0),          # naive datetime
    dt('2024-01-01T00:00:00').replace(tzinfo=None),
    0, None, [], date(2024, 1, 1),
])
def test_next_run_rejects_naive_or_unusable_after(after):
    with pytest.raises(ValueError) as excinfo:
        next_run({'frequency': 'daily', 'hour': 1}, after)
    assert str(excinfo.value) in SAFE_BACKUP_ERRORS


@pytest.mark.parametrize('after', ['2024-01-01T00:00:00', 'nonsense', '2024-13-01T00:00:00+00:00', ''])
def test_next_run_rejects_bad_after_string(after):
    with pytest.raises(ValueError) as excinfo:
        next_run({'frequency': 'daily', 'hour': 1}, after)
    assert str(excinfo.value) == '备份时间格式无效'


def test_next_run_rejects_invalid_plan():
    with pytest.raises(ValueError) as excinfo:
        next_run({'frequency': 'daily', 'timezone': 'Mars/Olympus'}, dt('2024-01-01T00:00:00+00:00'))
    assert str(excinfo.value) == '备份时区无效'
    with pytest.raises(ValueError) as excinfo:
        next_run({'frequency': 'daily', 'extra': 1}, dt('2024-01-01T00:00:00+00:00'))
    assert str(excinfo.value) == '备份计划参数无效'


def test_next_run_defaults_to_utc_when_timezone_omitted():
    assert next_run({'frequency': 'daily', 'hour': 6}, dt('2024-01-01T00:00:00+00:00')) == \
        '2024-01-01T06:00:00+00:00'
    # An explicitly present but unusable timezone is rejected, not defaulted.
    with pytest.raises(ValueError) as excinfo:
        next_run({'frequency': 'daily', 'timezone': None, 'hour': 6}, dt('2024-01-01T00:00:00+00:00'))
    assert str(excinfo.value) == '备份时区无效'


# --------------------------------------------------------------------------- #
# missed_slots
# --------------------------------------------------------------------------- #
def test_missed_slots_lists_slots_between_last_run_and_now():
    plan = {'frequency': 'daily', 'timezone': 'UTC', 'hour': 2, 'minute': 0}
    slots = missed_slots(plan, dt('2024-01-01T00:00:00+00:00'), dt('2024-01-04T00:00:00+00:00'))
    assert slots == ['2024-01-01T02:00:00+00:00', '2024-01-02T02:00:00+00:00',
                     '2024-01-03T02:00:00+00:00']


def test_missed_slots_includes_a_slot_exactly_at_now():
    plan = {'frequency': 'daily', 'timezone': 'UTC', 'hour': 2, 'minute': 0}
    assert missed_slots(plan, dt('2024-01-01T00:00:00+00:00'),
                        dt('2024-01-01T02:00:00+00:00')) == ['2024-01-01T02:00:00+00:00']


def test_missed_slots_none_last_run_is_empty():
    plan = {'frequency': 'daily', 'timezone': 'UTC', 'hour': 2}
    assert missed_slots(plan, None, dt('2024-01-04T00:00:00+00:00')) == []


def test_missed_slots_empty_when_no_slot_was_due():
    plan = {'frequency': 'weekly', 'timezone': 'UTC', 'hour': 2, 'weekday': 0}
    now = dt('2024-01-02T00:00:00+00:00')  # Tuesday; Monday slot already covered
    assert missed_slots(plan, dt('2024-01-01T02:00:00+00:00'), now) == []
    assert missed_slots(plan, now, now) == []
    assert missed_slots(plan, dt('2024-01-05T00:00:00+00:00'), now) == []


def test_missed_slots_truncates_at_limit():
    plan = {'frequency': 'daily', 'timezone': 'UTC', 'hour': 2, 'minute': 0}
    slots = missed_slots(plan, dt('2020-01-01T00:00:00+00:00'), dt('2024-01-01T00:00:00+00:00'))
    assert len(slots) == MAX_MISSED_SLOTS == 50
    assert slots[0] == '2020-01-01T02:00:00+00:00'
    assert slots[-1] == '2020-02-19T02:00:00+00:00'


def test_missed_slots_accepts_iso_strings_and_respects_timezone():
    plan = {'frequency': 'daily', 'timezone': 'Asia/Shanghai', 'hour': 2, 'minute': 0}
    slots = missed_slots(plan, '2024-01-10T00:00:00+08:00', '2024-01-11T00:00:00+08:00')
    assert slots == ['2024-01-10T02:00:00+08:00']
    with pytest.raises(ValueError) as excinfo:
        missed_slots(plan, 'bad-timestamp', '2024-01-11T00:00:00+08:00')
    assert str(excinfo.value) == '备份时间格式无效'


# --------------------------------------------------------------------------- #
# retention_plan
# --------------------------------------------------------------------------- #
FILES = [
    {'name': 'a.sqlite3', 'size': 1, 'created': '2024-01-01'},
    {'name': 'b.sqlite3', 'size': 2, 'created': '2024-01-03'},
    {'name': 'c.sqlite3', 'size': 4, 'created': '2024-01-02'},
]


def test_retention_plan_keeps_newest_and_reports_sizes():
    result = retention_plan(FILES, 2)
    assert [item['name'] for item in result['keep']] == ['b.sqlite3', 'c.sqlite3']
    assert [item['name'] for item in result['delete']] == ['a.sqlite3']
    assert result['total_size'] == 7
    assert result['keep_size'] == 6
    assert result['delete_size'] == 1
    assert result['keep_size'] + result['delete_size'] == result['total_size']


def test_retention_plan_invalid_created_is_deleted_first():
    files = FILES + [
        {'name': 'bad.sqlite3', 'size': 8, 'created': 'not-a-date'},
        {'name': 'missing.sqlite3', 'size': 16},
    ]
    result = retention_plan(files, 2)
    assert [item['name'] for item in result['keep']] == ['b.sqlite3', 'c.sqlite3']
    assert [item['name'] for item in result['delete']] == ['bad.sqlite3', 'missing.sqlite3', 'a.sqlite3']
    assert result['total_size'] == 31
    assert result['delete_size'] == 25


def test_retention_plan_normalises_entries():
    files = [{'name': None, 'size': -5, 'created': '2024-01-01'}]
    result = retention_plan(files, 1)
    assert result['keep'] == [{'name': '', 'size': 0, 'created': '2024-01-01'}]
    assert result['delete'] == []


def test_retention_plan_orders_mixed_offsets_by_instant():
    files = [
        {'name': 'utc.sqlite3', 'size': 1, 'created': '2024-01-01T00:00:00+00:00'},
        {'name': 'est.sqlite3', 'size': 2, 'created': '2023-12-31T20:00:00-05:00'},
    ]
    result = retention_plan(files, 1)
    assert [item['name'] for item in result['keep']] == ['est.sqlite3']
    assert [item['name'] for item in result['delete']] == ['utc.sqlite3']


def test_retention_plan_keep_larger_than_files_and_empty():
    result = retention_plan(FILES, 10)
    assert len(result['keep']) == 3 and result['delete'] == []
    empty = retention_plan([], 3)
    assert empty == {'keep': [], 'delete': [], 'total_size': 0, 'keep_size': 0, 'delete_size': 0}


@pytest.mark.parametrize('keep', [0, -1, 366, 1000, 2.0, '7', True, None])
def test_retention_plan_rejects_bad_keep(keep):
    with pytest.raises(ValueError) as excinfo:
        retention_plan(FILES, keep)
    assert str(excinfo.value) == '备份保留数量无效'


@pytest.mark.parametrize('files', ['a.sqlite3', {'name': 'a'}, None, 7, [1], ['a'], [None]])
def test_retention_plan_rejects_bad_file_list(files):
    with pytest.raises(ValueError) as excinfo:
        retention_plan(files, 2)
    assert str(excinfo.value) == '备份文件列表无效'


def test_retention_plan_never_mutates_or_deletes():
    files = [dict(item) for item in FILES]
    snapshot = [dict(item) for item in files]
    retention_plan(files, 1)
    assert files == snapshot  # advice only


# --------------------------------------------------------------------------- #
# drill_record
# --------------------------------------------------------------------------- #
def test_drill_record_uses_only_real_recovery_fields():
    record = drill_record('核心数据库', '2024-05-01', '通过', findings='缺口A',
                          operator='张三', evidence='日志A', next_drill='2024-11-01')
    assert set(record) <= RECOVERY_FIELDS
    assert record == {
        '名称': '备份恢复演练：核心数据库',
        '负责人': '张三',
        '备份范围': '核心数据库',
        '演练日期': '2024-05-01',
        '实际数据缺口': '缺口A',
        '结论': '通过',
        '证据': '日志A',
    }
    assert '下一步' not in record and 'next_drill' not in record


def test_drill_record_title_without_scope_and_default_optionals():
    record = drill_record('', '2024-05-01', '部分通过')
    assert record['名称'] == '备份恢复演练'
    assert record['备份范围'] == ''
    assert record['负责人'] == ''
    assert record['实际数据缺口'] == ''
    assert record['证据'] == ''
    assert record['结论'] == '部分通过'
    assert set(record) <= RECOVERY_FIELDS


@pytest.mark.parametrize('result', DRILL_RESULTS)
def test_drill_record_accepts_every_allowed_result(result):
    assert drill_record('范围', '2024-05-01', result)['结论'] == result


@pytest.mark.parametrize('result', ['pass', '通过 ', '未通过', '', None, 1, True])
def test_drill_record_rejects_bad_result(result):
    with pytest.raises(ValueError) as excinfo:
        drill_record('范围', '2024-05-01', result)
    assert str(excinfo.value) == '恢复演练字段无效'


@pytest.mark.parametrize('drilled_on', ['2024-5-1', '2024/05/01', '2024-02-30', '20240501',
                                        '2024-13-01', '', None, 20240501, date(2024, 5, 1)])
def test_drill_record_rejects_bad_drill_date(drilled_on):
    with pytest.raises(ValueError) as excinfo:
        drill_record('范围', drilled_on, '通过')
    assert str(excinfo.value) == '恢复演练日期无效'


@pytest.mark.parametrize('next_drill', ['2024-11-1', '2024-02-30', 'tomorrow', 20241101, ''])
def test_drill_record_validates_next_drill(next_drill):
    with pytest.raises(ValueError) as excinfo:
        drill_record('范围', '2024-05-01', '通过', next_drill=next_drill)
    assert str(excinfo.value) == '恢复演练日期无效'
    assert drill_record('范围', '2024-05-01', '通过', next_drill=None)['结论'] == '通过'
    assert drill_record('范围', '2024-05-01', '通过', next_drill='2024-11-01')['结论'] == '通过'


@pytest.mark.parametrize('kwargs', [
    {'plan_scope': 5}, {'findings': 5}, {'operator': 5}, {'evidence': []},
])
def test_drill_record_rejects_non_string_optionals(kwargs):
    call = {'plan_scope': '范围', 'drilled_on': '2024-05-01', 'result': '通过'}
    call.update(kwargs)
    with pytest.raises(ValueError) as excinfo:
        drill_record(**call)
    assert str(excinfo.value) == '恢复演练字段无效'


# --------------------------------------------------------------------------- #
# drill_reminder
# --------------------------------------------------------------------------- #
def recovery_record(record_id, drilled_on, title='演练', **extra):
    data = {'名称': title}
    if drilled_on is not None:
        data['演练日期'] = drilled_on
    data.update(extra)
    return {'id': record_id, 'module': 'recovery', 'data': data}


@pytest.mark.parametrize('days_since,severity', [
    (0, None), (22, None), (23, '低'), (24, '低'), (25, '低'),
    (27, '中'), (29, '中'), (30, '中'), (31, '高'), (60, '高'),
])
def test_drill_reminder_thresholds(days_since, severity):
    today = date(2024, 5, 31)
    drilled = today - timedelta(days=days_since)
    items = drill_reminder([recovery_record('R1', drilled.isoformat())], today, warn_days=30)
    if severity is None:
        assert items == []
        return
    assert len(items) == 1
    item = items[0]
    assert item['severity'] == severity
    assert item['days_left'] == 30 - days_since
    assert item['date'] == drilled.isoformat()
    assert item['record_id'] == 'R1'
    assert item['title'] == '演练'
    assert isinstance(item['message'], str) and item['message']


def test_drill_reminder_exactly_warn_days_is_due_not_overdue():
    today = date(2024, 5, 31)
    item = drill_reminder([recovery_record('R', '2024-05-01')], today, warn_days=30)[0]
    assert item['days_left'] == 0 and item['severity'] == '中' and '到期' in item['message']
    overdue = drill_reminder([recovery_record('R', '2024-04-30')], today, warn_days=30)[0]
    assert overdue['days_left'] == -1 and overdue['severity'] == '高'
    assert '超过 30 天' in overdue['message']


def test_drill_reminder_skips_missing_or_invalid_dates():
    records = [
        recovery_record('no-date', None),
        recovery_record('bad-date', '2024-13-01'),
        recovery_record('loose-date', '2024/05/01'),
        {'id': 'raw-date-only', 'date': '2024-01-01'},  # not the real field -> skip, never guess
        'not-a-dict',
        None,
        recovery_record('ok', '2024-05-01'),
    ]
    items = drill_reminder(records, date(2024, 5, 31), warn_days=30)
    assert [item['record_id'] for item in items] == ['ok']


def test_drill_reminder_accepts_plain_data_dicts_and_date_objects():
    today = date(2024, 5, 31)
    plain = [{'名称': '纯数据', '演练日期': '2024-05-01'}]
    assert drill_reminder(plain, today) == drill_reminder([recovery_record('', '2024-05-01', title='纯数据')], today)
    with_date_object = [{'名称': '对象日期', '演练日期': date(2024, 5, 1)}]
    assert drill_reminder(with_date_object, today)[0]['date'] == '2024-05-01'


def test_drill_reminder_sorts_by_urgency():
    today = date(2024, 5, 31)
    records = [recovery_record('soon', '2024-05-06'),   # days_left 4 -> 低
               recovery_record('late', '2024-03-01'),   # long overdue -> 高
               recovery_record('due', '2024-05-01')]    # days_left 0 -> 中
    items = drill_reminder(records, today, warn_days=30)
    assert [item['record_id'] for item in items] == ['late', 'due', 'soon']
    assert [item['severity'] for item in items] == ['高', '中', '低']


@pytest.mark.parametrize('today', ['2024-05-31', None, 20240531, 'today', 0])
def test_drill_reminder_requires_a_date(today):
    with pytest.raises(ValueError) as excinfo:
        drill_reminder([recovery_record('R', '2024-05-01')], today, warn_days=30)
    assert str(excinfo.value) == '恢复演练日期无效'


def test_drill_reminder_accepts_datetime_today():
    items = drill_reminder([recovery_record('R', '2024-05-01')],
                           datetime(2024, 5, 31, 23, 30), warn_days=30)
    assert items[0]['days_left'] == 0


@pytest.mark.parametrize('warn_days', [0, -1, 3651, 30.0, '30', True, None])
def test_drill_reminder_rejects_bad_warn_days(warn_days):
    with pytest.raises(ValueError) as excinfo:
        drill_reminder([], date(2024, 5, 31), warn_days=warn_days)
    assert str(excinfo.value) == '恢复演练字段无效'


@pytest.mark.parametrize('records', ['R1', {'id': 'R1'}, None, 7])
def test_drill_reminder_rejects_bad_record_list(records):
    with pytest.raises(ValueError) as excinfo:
        drill_reminder(records, date(2024, 5, 31))
    assert str(excinfo.value) == '恢复演练字段无效'


# --------------------------------------------------------------------------- #
# backup_health
# --------------------------------------------------------------------------- #
def test_backup_health_ok_when_fresh_and_no_slot_missed():
    plan = {'enabled': True, 'frequency': 'daily', 'timezone': 'Asia/Shanghai',
            'hour': 2, 'minute': 0}
    files = [{'name': 'itmanager-20240110.sqlite3', 'size': 10,
              'created': '2024-01-10T03:00:00+08:00'}]
    result = backup_health(files, plan, dt('2024-01-10T04:00:00+08:00'), max_age_days=7)
    assert result == {'ok': True, 'reasons': [], 'newest': 'itmanager-20240110.sqlite3',
                      'age_days': 0, 'missed': 0}


def test_backup_health_without_files():
    plan = {'enabled': True, 'frequency': 'daily', 'timezone': 'Asia/Shanghai', 'hour': 2}
    result = backup_health([], plan, dt('2024-01-10T04:00:00+08:00'))
    assert result['ok'] is False
    assert result['reasons'] == ['没有可用的备份文件']
    assert result['newest'] is None and result['age_days'] is None and result['missed'] == 0


def test_backup_health_with_unusable_created_values():
    plan = {'enabled': True, 'frequency': 'daily', 'timezone': 'UTC', 'hour': 2}
    files = [{'name': 'a.sqlite3', 'size': 1, 'created': 'not-a-date'},
             {'name': 'b.sqlite3', 'size': 1}]
    result = backup_health(files, plan, dt('2024-01-10T04:00:00+00:00'))
    assert result['ok'] is False
    assert result['reasons'] == ['没有可用的备份文件（备份时间无效）']
    assert result['newest'] is None and result['age_days'] is None and result['missed'] == 0


def test_backup_health_stale_backup_reason():
    plan = {'enabled': False, 'frequency': 'daily', 'timezone': 'UTC', 'hour': 2}
    files = [{'name': 'old.sqlite3', 'size': 1, 'created': '2024-01-01T03:00:00+00:00'}]
    result = backup_health(files, plan, dt('2024-01-15T04:00:00+00:00'), max_age_days=7)
    assert result['ok'] is False
    assert result['age_days'] == 14 and result['newest'] == 'old.sqlite3'
    assert result['reasons'] == ['最新备份已超过 7 天（14 天）']
    # 14 missed daily slots are still counted, but a disabled plan adds no reason.
    assert result['missed'] == 14


def test_backup_health_age_boundary_is_inclusive():
    plan = {'enabled': False, 'frequency': 'daily', 'timezone': 'UTC', 'hour': 2}
    files = [{'name': 'x.sqlite3', 'size': 1, 'created': '2024-01-08T00:00:00+00:00'}]
    fresh = backup_health(files, plan, dt('2024-01-15T00:00:00+00:00'), max_age_days=7)
    assert fresh['age_days'] == 7 and fresh['ok'] is True and fresh['reasons'] == []
    stale = backup_health(files, plan, dt('2024-01-15T01:00:00+00:00'), max_age_days=7)
    assert stale['age_days'] == 7 and stale['ok'] is True  # age uses the local date


def test_backup_health_missed_slots_when_plan_enabled():
    files = [{'name': 'b.sqlite3', 'size': 1, 'created': '2024-01-10T03:00:00+08:00'}]
    now = dt('2024-01-12T04:00:00+08:00')
    enabled = dict(DAILY_SH, enabled=True)
    result = backup_health(files, enabled, now, max_age_days=7)
    assert result['missed'] == 2 and result['age_days'] == 2
    assert result['ok'] is False
    assert result['reasons'] == ['备份计划已启用但错过了 2 个备份时刻']
    disabled = dict(enabled, enabled=False)
    result_disabled = backup_health(files, disabled, now, max_age_days=7)
    assert result_disabled['missed'] == 2 and result_disabled['reasons'] == []
    assert result_disabled['ok'] is True


def test_backup_health_combines_all_reasons():
    plan = {'enabled': True, 'frequency': 'daily', 'timezone': 'UTC', 'hour': 2}
    files = [{'name': 'old.sqlite3', 'size': 1, 'created': '2024-01-01T00:00:00+00:00'}]
    result = backup_health(files, plan, dt('2024-01-20T00:00:00+00:00'), max_age_days=3)
    assert result['ok'] is False
    assert len(result['reasons']) == 2
    assert result['reasons'][0].startswith('最新备份已超过')
    assert result['reasons'][1].startswith('备份计划已启用但错过了')
    assert result['age_days'] == 19 and result['missed'] == 19


def test_backup_health_picks_newest_file_and_ignores_invalid_ones():
    plan = {'enabled': True, 'frequency': 'daily', 'timezone': 'UTC', 'hour': 2}
    files = [{'name': 'bad.sqlite3', 'size': 1, 'created': 'oops'},
             {'name': 'mid.sqlite3', 'size': 1, 'created': '2024-01-09T03:00:00+00:00'},
             {'name': 'new.sqlite3', 'size': 1, 'created': '2024-01-10T03:00:00+00:00'}]
    result = backup_health(files, plan, dt('2024-01-10T04:00:00+00:00'), max_age_days=7)
    assert result['newest'] == 'new.sqlite3'
    assert result['ok'] is True


@pytest.mark.parametrize('files', ['a.sqlite3', {'name': 'a'}, None, 7, [1]])
def test_backup_health_rejects_bad_files(files):
    plan = {'frequency': 'daily', 'timezone': 'UTC', 'hour': 2}
    with pytest.raises(ValueError) as excinfo:
        backup_health(files, plan, dt('2024-01-10T04:00:00+00:00'))
    assert str(excinfo.value) == '备份文件列表无效'


@pytest.mark.parametrize('bad', [
    {'now': datetime(2024, 1, 10, 4, 0, 0)},          # naive now
    {'now': 'not-a-time'},
    {'max_age_days': 0},
    {'max_age_days': -1},
    {'max_age_days': 7.0},
    {'max_age_days': True},
])
def test_backup_health_rejects_bad_parameters(bad):
    plan = {'frequency': 'daily', 'timezone': 'UTC', 'hour': 2}
    files = [{'name': 'a.sqlite3', 'size': 1, 'created': '2024-01-10T03:00:00+00:00'}]
    kwargs = {'now': dt('2024-01-10T04:00:00+00:00'), 'max_age_days': 7}
    kwargs.update(bad)
    with pytest.raises(ValueError) as excinfo:
        backup_health(files, plan, **kwargs)
    assert str(excinfo.value) in ('备份计划参数无效', '备份时间格式无效')


def test_backup_health_validates_the_plan():
    files = [{'name': 'a.sqlite3', 'size': 1, 'created': '2024-01-10T03:00:00+00:00'}]
    with pytest.raises(ValueError) as excinfo:
        backup_health(files, {'frequency': 'daily', 'unknown': 1}, dt('2024-01-10T04:00:00+00:00'))
    assert str(excinfo.value) == '备份计划参数无效'


# --------------------------------------------------------------------------- #
# error vocabulary
# --------------------------------------------------------------------------- #
REQUIRED_MESSAGES = (
    '备份计划参数无效', '备份频率无效', '备份时区无效', '备份保留数量无效',
    '备份文件列表无效', '备份时间格式无效', '恢复演练字段无效', '恢复演练日期无效',
)


def test_safe_backup_errors_is_a_frozenset_with_required_messages():
    assert isinstance(SAFE_BACKUP_ERRORS, frozenset)
    assert set(REQUIRED_MESSAGES) <= SAFE_BACKUP_ERRORS


BAD_CALLS = [
    lambda: validate_plan({'frequency': 'daily', 'nope': 1}),
    lambda: validate_plan({'frequency': 'hourly'}),
    lambda: validate_plan({'frequency': 'daily', 'timezone': 'Mars/Olympus'}),
    lambda: validate_plan({'frequency': 'daily', 'keep': 0}),
    lambda: validate_plan({'frequency': 'weekly'}),
    lambda: validate_plan({'frequency': 'monthly'}),
    lambda: next_run({'frequency': 'daily', 'hour': 1}, datetime(2024, 1, 1)),
    lambda: next_run({'frequency': 'daily', 'hour': 1}, 'no-such-time'),
    lambda: next_run({'frequency': 'daily', 'hour': 1}, '2024-01-01T00:00:00'),
    lambda: next_run({'frequency': 'daily', 'timezone': 'Mars/Olympus'}, dt('2024-01-01T00:00:00+00:00')),
    lambda: missed_slots({'frequency': 'daily', 'hour': 1}, 'bad', 'also-bad'),
    lambda: retention_plan([], 0),
    lambda: retention_plan('not-a-list', 1),
    lambda: retention_plan([1], 1),
    lambda: drill_record('s', '2024-5-1', '通过'),
    lambda: drill_record('s', '2024-05-01', 'pass'),
    lambda: drill_record('s', '2024-05-01', '通过', next_drill='x'),
    lambda: drill_reminder([], '2024-05-01'),
    lambda: drill_reminder([], date(2024, 5, 31), warn_days=0),
    lambda: drill_reminder('nope', date(2024, 5, 31)),
    lambda: backup_health('nope', {'frequency': 'daily'}, dt('2024-01-01T00:00:00+00:00')),
    lambda: backup_health([], {'frequency': 'daily'}, dt('2024-01-01T00:00:00+00:00'), max_age_days=0),
]


@pytest.mark.parametrize('call', BAD_CALLS)
def test_every_raised_message_is_safe(call):
    with pytest.raises(ValueError) as excinfo:
        call()
    assert str(excinfo.value) in SAFE_BACKUP_ERRORS
