"""Reminder rules: pure, offline, deterministic; every fixture is fictional."""
import json
from datetime import date, datetime, timedelta

import pytest

from itmanager import reminders
from itmanager.schema import MODULES

TODAY = date(2026, 6, 1)


def rec(module, data, oid='REC-1', updated='2026-05-31T00:00:00+00:00', archived=0):
    return {'id': oid, 'module': module, 'data': data, 'version': 1,
            'updated': updated, 'archived': archived}


def rule(**overrides):
    base = {'key': 'k1', 'name': '规则', 'type': 'expiry', 'modules': ['contracts'],
            'date_fields': ['到期日期'], 'warn_days': 30, 'stale_days': 90,
            'severity': '中', 'enabled': True, 'note': ''}
    base.update(overrides)
    return base


def days(offset):
    return (TODAY + timedelta(days=offset)).isoformat()


def test_default_rules_are_fresh_valid_and_cover_required_scenarios():
    rules = reminders.default_rules()
    assert len(rules) >= 6
    keys = [item['key'] for item in rules]
    assert len(keys) == len(set(keys))
    for item in rules:
        reminders.validate_rule(item)
        assert item['modules'] and set(item['modules']) <= set(MODULES)
        assert item['severity'] in reminders.SEVERITIES
        assert json.dumps(item, ensure_ascii=False)
    assert {'contract_expiry', 'asset_warranty_expiry', 'access_review_expiry',
            'recovery_drill_expiry', 'review_cycle_expiry', 'risk_review_expiry',
            'task_overdue', 'stale_open_record'} <= set(keys)
    assert {item['type'] for item in rules} == {'expiry', 'overdue', 'stale'}
    rules[0]['modules'].append('unknown_module')
    rules[0]['date_fields'].append('unknown_field')
    fresh = reminders.default_rules()
    assert 'unknown_module' not in fresh[0]['modules']
    assert 'unknown_field' not in fresh[0]['date_fields']
    assert reminders.default_rules() == fresh


@pytest.mark.parametrize('patch,message', [
    ({'extra': 1}, '提醒规则包含未知字段'),
    ({'key': '1bad'}, '提醒规则key无效'),
    ({'key': ''}, '提醒规则key无效'),
    ({'key': 7}, '提醒规则key无效'),
    ({'name': '   '}, '提醒规则名称无效'),
    ({'name': None}, '提醒规则名称无效'),
    ({'type': 'boom'}, '提醒规则类型无效'),
    ({'modules': []}, '提醒规则的业务模块无效'),
    ({'modules': ['nope']}, '提醒规则的业务模块无效'),
    ({'modules': ['contracts', 'contracts']}, '提醒规则的业务模块无效'),
    ({'date_fields': ['不存在字段']}, '提醒规则日期字段不符合模块定义'),
    ({'date_fields': ['维保到期日期']}, '提醒规则日期字段不符合模块定义'),
    ({'date_fields': []}, '提醒规则日期字段不符合模块定义'),
    ({'warn_days': 366}, '提醒提前天数须为0—365'),
    ({'warn_days': -1}, '提醒提前天数须为0—365'),
    ({'warn_days': True}, '提醒提前天数须为0—365'),
    ({'warn_days': '30'}, '提醒提前天数须为0—365'),
    ({'stale_days': 0}, '提醒未更新天数须为1—3650'),
    ({'stale_days': 3651}, '提醒未更新天数须为1—3650'),
    ({'stale_days': None}, '提醒未更新天数须为1—3650'),
    ({'severity': '紧急'}, '提醒等级无效'),
    ({'severity': None}, '提醒等级无效'),
    ({'enabled': 1}, '提醒规则启用状态须为布尔值'),
    ({'enabled': 'true'}, '提醒规则启用状态须为布尔值'),
    ({'note': 5}, '提醒规则包含未知字段'),
])
def test_validate_rule_rejects_each_invalid_class(patch, message):
    assert reminders.validate_rule(rule()) is None
    broken = rule(**patch)
    with pytest.raises(ValueError) as excinfo:
        reminders.validate_rule(broken)
    assert str(excinfo.value) == message
    assert str(excinfo.value) in reminders.SAFE_REMINDER_ERRORS


@pytest.mark.parametrize('missing', ['key', 'name', 'type', 'modules', 'date_fields',
                                     'warn_days', 'stale_days', 'severity', 'enabled'])
def test_validate_rule_rejects_missing_required_keys(missing):
    broken = rule()
    broken.pop(missing)
    with pytest.raises(ValueError) as excinfo:
        reminders.validate_rule(broken)
    assert str(excinfo.value) in reminders.SAFE_REMINDER_ERRORS


def test_validate_rule_rejects_non_mapping_and_never_interpolates_input():
    for broken in (None, [], 'rule', 42):
        with pytest.raises(ValueError) as excinfo:
            reminders.validate_rule(broken)
        assert str(excinfo.value) in reminders.SAFE_REMINDER_ERRORS
    leaky = rule(name='<script>alert(1)</script>', key='bad key with spaces')
    with pytest.raises(ValueError) as excinfo:
        reminders.validate_rule(leaky)
    assert 'script' not in str(excinfo.value) and 'spaces' not in str(excinfo.value)


def test_expiry_hits_on_warn_day_and_misses_the_next_day():
    expiry = rule(warn_days=30)
    hit = reminders.compute_items([rec('contracts', {'名称': '合同A', '到期日期': days(30)})],
                                 [expiry], TODAY)
    assert len(hit) == 1
    assert hit[0]['days_left'] == 30 and hit[0]['overdue_days'] == 0
    assert hit[0]['date'] == days(30) and hit[0]['module'] == 'contracts'
    miss = reminders.compute_items([rec('contracts', {'名称': '合同B', '到期日期': days(31)})],
                                  [expiry], TODAY)
    assert miss == []


def test_expiry_expired_days_escalate_severity():
    low = rule(severity='低', warn_days=90)
    five = reminders.compute_items(
        [rec('contracts', {'名称': '已过期', '到期日期': days(-5)})], [low], TODAY)[0]
    assert (five['severity'], five['days_left'], five['overdue_days']) == ('中', -5, 5)
    assert '已过期 5 天' in five['message']
    forty = reminders.compute_items(
        [rec('contracts', {'名称': '久拖', '到期日期': days(-40)})], [low], TODAY)[0]
    assert (forty['severity'], forty['days_left'], forty['overdue_days']) == ('高', -40, 40)
    one = reminders.compute_items(
        [rec('contracts', {'名称': '刚过期', '到期日期': days(-1)})],
        [rule(severity='高', warn_days=90)], TODAY)[0]
    assert one['severity'] == '高'


def test_expiry_rule_without_date_value_is_skipped():
    assert reminders.compute_items([rec('contracts', {'名称': '无日期'})], [rule()], TODAY) == []


def test_overdue_ignores_finished_and_archived_records():
    overdue = rule(key='task_overdue', name='任务逾期', type='overdue', modules=['tasks'],
                   date_fields=['截止日期'], warn_days=0, severity='高')
    past = days(-3)
    rows = [rec('tasks', {'名称': '逾期中', '截止日期': past, '状态': '进行中'}, oid='T-1'),
            rec('tasks', {'名称': '已完成', '截止日期': past, '状态': '已完成'}, oid='T-2'),
            rec('tasks', {'名称': '已取消', '截止日期': past, '状态': '已取消'}, oid='T-3'),
            rec('tasks', {'名称': '已关闭', '截止日期': past, '状态': '已关闭'}, oid='T-4'),
            rec('tasks', {'名称': '已定稿', '截止日期': past, '状态': '已定稿'}, oid='T-5'),
            rec('tasks', {'名称': '已归档', '截止日期': past, '状态': '进行中'}, oid='T-6', archived=1)]
    items = reminders.compute_items(rows, [overdue], TODAY)
    assert [item['record_id'] for item in items] == ['T-1']
    assert (items[0]['days_left'], items[0]['overdue_days']) == (-3, 3)
    assert items[0]['rule_key'] == 'task_overdue' and items[0]['rule_name'] == '任务逾期'
    assert reminders.compute_items(
        [rec('tasks', {'名称': '今日到期', '截止日期': days(0), '状态': '进行中'})], [overdue], TODAY) == []


def test_stale_boundary_and_finished_status():
    stale = rule(key='stale', name='长期未更新', type='stale', modules=['tasks'],
                 date_fields=[], warn_days=0, stale_days=30, severity='低')
    def stale_row(age, oid, status='进行中'):
        return rec('tasks', {'名称': '任务' + oid, '状态': status}, oid=oid,
                   updated=(TODAY - timedelta(days=age)).isoformat() + 'T00:00:00+00:00')
    assert reminders.compute_items([stale_row(30, 'T-1')], [stale], TODAY) == []
    hit = reminders.compute_items([stale_row(31, 'T-2')], [stale], TODAY)[0]
    assert (hit['days_left'], hit['overdue_days'], hit['date']) == (-31, 31, (TODAY - timedelta(days=31)).isoformat())
    assert reminders.compute_items([stale_row(400, 'T-3', '已完成')], [stale], TODAY) == []
    assert reminders.compute_items([stale_row(400, 'T-4', '已取消')], [stale], TODAY) == []
    assert reminders.compute_items(
        [rec('tasks', {'名称': '坏时间', '状态': '进行中'}, updated='not-a-time')], [stale], TODAY) == []


def test_same_record_keeps_highest_severity_then_nearest_date():
    near = rule(key='near', name='近', modules=['assets'], date_fields=['采购日期'],
                warn_days=365, severity='中')
    far = rule(key='far', name='远', modules=['assets'], date_fields=['维保到期日期'],
               warn_days=365, severity='中')
    strong = rule(key='strong', name='强', modules=['assets'], date_fields=['维保到期日期'],
                  warn_days=365, severity='高')
    row = rec('assets', {'名称': '资产A', '采购日期': days(-1), '维保到期日期': days(5)})
    tie = reminders.compute_items([row], [near, far], TODAY)
    assert len(tie) == 1
    assert (tie[0]['rule_key'], tie[0]['days_left']) == ('near', -1)
    highest = reminders.compute_items([row], [near, strong], TODAY)
    assert len(highest) == 1
    assert (highest[0]['rule_key'], highest[0]['severity']) == ('strong', '高')


def test_items_sort_by_severity_then_days_then_module_then_title():
    high = rule(key='high', name='高', warn_days=365, severity='高')
    low = rule(key='low', name='低', warn_days=365, severity='低')
    stale = rule(key='stale', name='未更新', type='stale', modules=['tasks'], date_fields=[],
                 warn_days=0, stale_days=10, severity='低')
    rows = [rec('contracts', {'名称': 'Z合同', '到期日期': days(10)}, oid='C-1'),
            rec('contracts', {'名称': 'A合同', '到期日期': days(10)}, oid='C-2'),
            rec('contracts', {'名称': 'B合同', '到期日期': days(9)}, oid='C-3'),
            rec('tasks', {'名称': '任务', '状态': '进行中'}, oid='T-1',
                updated=(TODAY - timedelta(days=20)).isoformat() + 'T00:00:00+00:00')]
    items = reminders.compute_items(rows, [high, low, stale], TODAY)
    assert [item['title'] for item in items] == ['B合同', 'A合同', 'Z合同', '任务']
    assert [item['severity'] for item in items] == ['高', '高', '高', '低']
    assert [item['sort'] for item in items] == sorted(item['sort'] for item in items)
    assert reminders.compute_items(rows, [high, low, stale], TODAY) == items


def test_archived_records_are_ignored_for_every_rule_type():
    expiry = rule(warn_days=365, severity='高')
    overdue = rule(key='task_overdue', name='逾期', type='overdue', modules=['tasks'],
                   date_fields=['截止日期'], warn_days=0, severity='高')
    stale = rule(key='stale', name='未更新', type='stale', modules=['tasks'], date_fields=[],
                 warn_days=0, stale_days=1, severity='低')
    rows = [rec('contracts', {'名称': '归档合同', '到期日期': days(-99)}, oid='C-1', archived=1),
            rec('tasks', {'名称': '归档逾期', '截止日期': days(-99), '状态': '进行中'},
                oid='T-1', archived=1, updated=(TODAY - timedelta(days=999)).isoformat()),
            rec('tasks', {'名称': '归档未更新', '状态': '进行中'}, oid='T-2', archived=1,
                updated=(TODAY - timedelta(days=999)).isoformat())]
    assert reminders.compute_items(rows, [expiry, overdue, stale], TODAY) == []


def test_bad_dates_and_malformed_records_never_crash():
    rows = [rec('contracts', {'名称': '坏日期', '到期日期': 'not-a-date'}),
            rec('contracts', {'名称': '短日期', '到期日期': '2026-1-1'}),
            rec('contracts', {'名称': '不存在日期', '到期日期': '2026-02-30'}),
            rec('contracts', {'名称': '非文本日期', '到期日期': 20260601}),
            rec('contracts', {'名称': '无日期'}),
            rec('unknown_module', {'名称': '未知模块', '到期日期': days(-5)}),
            {'id': 'BAD-1', 'module': 'contracts', 'data': 'not-a-dict', 'updated': None},
            {'id': '', 'module': 'contracts', 'data': {'到期日期': days(-5)}},
            {'module': 'contracts'},
            'not-a-record']
    assert reminders.compute_items(rows, [rule(warn_days=365)], TODAY) == []


def test_today_type_and_input_shape_are_validated():
    for bad_today in ('2026-06-01', datetime(2026, 6, 1, 8, 30), None, 20260601):
        with pytest.raises(ValueError) as excinfo:
            reminders.compute_items([], [], bad_today)
        assert str(excinfo.value) == '提醒扫描日期无效'
        assert str(excinfo.value) in reminders.SAFE_REMINDER_ERRORS
    with pytest.raises(ValueError) as excinfo:
        reminders.compute_items('records', [], TODAY)
    assert str(excinfo.value) == '提醒输入记录格式无效'
    with pytest.raises(ValueError) as excinfo:
        reminders.compute_items([], {'key': 'k1'}, TODAY)
    assert str(excinfo.value) == '提醒规则结构无效'


def test_disabled_and_invalid_rules_are_skipped():
    rows = [rec('contracts', {'名称': '合同A', '到期日期': days(-5)})]
    off = rule(key='off', warn_days=365)
    off['enabled'] = False
    assert reminders.compute_items(rows, [off], TODAY) == []
    broken = rule(key='broken', warn_days=999)
    kept = rule(key='kept', name='保留', warn_days=365, severity='中')
    items = reminders.compute_items(rows, [broken, 'not-a-rule', kept], TODAY)
    assert [item['rule_key'] for item in items] == ['kept']


def test_summarize_counts_by_severity_module_and_rule():
    high = rule(key='high', name='高', warn_days=365, severity='高')
    medium = rule(key='medium', name='中', modules=['assets'], date_fields=['维保到期日期'],
                  warn_days=365, severity='中')
    stale = rule(key='stale', name='未更新', type='stale', modules=['tasks'], date_fields=[],
                 warn_days=0, stale_days=10, severity='低')
    rows = [rec('contracts', {'名称': '合同A', '到期日期': days(-1)}, oid='C-1'),
            rec('assets', {'名称': '资产A', '维保到期日期': days(-2)}, oid='A-1'),
            rec('assets', {'名称': '资产B', '维保到期日期': days(-3)}, oid='A-2'),
            rec('tasks', {'名称': '任务A', '状态': '进行中'}, oid='T-1',
                updated=(TODAY - timedelta(days=20)).isoformat() + 'T00:00:00+00:00')]
    items = reminders.compute_items(rows, [high, medium, stale], TODAY)
    summary = reminders.summarize(items)
    assert summary['total'] == 4
    assert (summary['high'], summary['medium'], summary['low']) == (1, 2, 1)
    assert summary['by_module'] == {'contracts': 1, 'assets': 2, 'tasks': 1}
    assert summary['by_rule'] == {'high': 1, 'medium': 2, 'stale': 1}
    assert reminders.summarize([]) == {'total': 0, 'high': 0, 'medium': 0, 'low': 0,
                                       'by_module': {}, 'by_rule': {}}


def test_items_are_json_serializable_with_documented_keys():
    high = rule(key='high', name='高', warn_days=365, severity='高')
    items = reminders.compute_items([rec('contracts', {'名称': '合同A', '到期日期': days(-1)})],
                                   [high], TODAY)
    assert len(items) == 1
    expected = {'rule_key', 'rule_name', 'severity', 'module', 'record_id', 'title', 'date',
                'days_left', 'overdue_days', 'message', 'sort'}
    assert set(items[0]) == expected
    assert isinstance(items[0]['sort'], list) and json.dumps(items, ensure_ascii=False)
    assert items[0]['sort'] == [0, -1, 'contracts', '合同A']
