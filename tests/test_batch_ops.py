"""Batch operations and filtered export: pure planning, no IO, deterministic.

All fixtures are fictional. The plan_* helpers must never touch their inputs, so most
tests compare the supplied records against a deep copy taken beforehand.
"""
import copy
import csv
import io
from datetime import date, datetime

import pytest

from itmanager.batch_ops import (
    ENDED_STATUSES, POSTPONE_FIELDS, REVIEW_STATUS, SAFE_BATCH_ERRORS, batchable_fields,
    export_rows, plan_archive, plan_field_update, plan_postpone, safe_filename, to_csv,
)
from itmanager.schema import MODULES

TODAY = date(2026, 6, 1)


def rec(module, data, oid='X-1', version=1, archived=0):
    return {'id': oid, 'module': module, 'data': dict(data), 'version': version,
            'created': '2026-01-01T00:00:00+00:00', 'updated': '2026-01-01T00:00:00+00:00',
            'archived': archived}


def reasons(plan):
    return [item['reason'] for item in plan['skipped']]


def changed_ids(plan):
    return [change['id'] for change in plan['changes']]


def skipped_ids(plan):
    return [item['id'] for item in plan['skipped']]


# --- whitelist -------------------------------------------------------------------

def test_batchable_fields_follow_schema_order_for_every_module():
    for module, (_, fields) in MODULES.items():
        allowed = batchable_fields(module)
        assert allowed == [name for name in fields if name in allowed], module
        assert len(set(allowed)) == len(allowed)
        assert fields[0] not in allowed
        assert not [name for name in allowed if name.endswith('ID')]
        assert not [name for name in allowed
                    if any(word in name for word in ('证据', '评价', '反馈', '结论', '自评', '复盘', '根因'))]
        assert '受理时间' not in allowed and '恢复时间' not in allowed


def test_batchable_fields_expected_members_per_module():
    assert batchable_fields('tasks') == ['负责人', '优先级', '开始日期', '截止日期', '状态', '下一步',
                                         '阻塞原因', '验收人', '任务目标', '执行步骤', '验收标准',
                                         '风险与依赖', '管理建议']
    assert batchable_fields('reviews') == ['考核周期', '考核目标', '指标口径', '权重说明', '评分', '复核人', '状态']
    assert batchable_fields('incidents') == ['影响范围', '级别', '负责人', '状态', '处置过程', '业务确认']
    assert batchable_fields('people') == ['部门', '岗位', '主管', '邮箱', '职责', '能力发展计划', '状态']
    assert batchable_fields('logs') == ['日期', '工作内容', '结果', '下一步']


def test_unknown_module_raises_the_fixed_batch_error():
    with pytest.raises(ValueError, match='批量操作模块无效'):
        batchable_fields('nope')
    with pytest.raises(ValueError, match='批量操作模块无效'):
        plan_field_update([], 'nope', '负责人', '张三')
    with pytest.raises(ValueError, match='批量操作模块无效'):
        plan_postpone([], 'nope', 1)
    with pytest.raises(ValueError, match='批量导出模块无效'):
        export_rows([], 'nope')
    with pytest.raises(ValueError, match='批量操作模块无效'):
        batchable_fields(None)


# --- plan_field_update -----------------------------------------------------------

def test_plan_field_update_returns_complete_data_and_version():
    row = rec('tasks', {'名称': '任务A', '负责人': '张三', '状态': '进行中', '完成证据': ''}, 'T-1', version=4)
    plan = plan_field_update([row], 'tasks', '负责人', '李四')
    assert plan['summary'] == {'module': 'tasks', 'field': '负责人', 'value': '李四',
                               'total': 1, 'changed': 1, 'skipped': 0}
    assert plan['skipped'] == []
    change = plan['changes'][0]
    assert set(change) == {'id', 'version', 'data'}  # exact keys of POST /records/batch
    assert change['id'] == 'T-1' and change['version'] == 4
    assert change['data'] == {'名称': '任务A', '负责人': '李四', '状态': '进行中', '完成证据': ''}
    assert change['data'] is not row['data']
    assert row['data']['负责人'] == '张三'


def test_plan_field_update_skips_a_field_outside_the_whitelist():
    rows = [rec('tasks', {'名称': 'A', '完成证据': ''}, 'T-1'),
            rec('tasks', {'名称': 'B', '完成证据': ''}, 'T-2')]
    for field in ('名称', '完成证据', '项目ID', '受理时间'):
        plan = plan_field_update(rows, 'tasks', field, '已验收')
        assert plan['changes'] == []
        assert reasons(plan) == ['该字段不允许批量修改'] * 2
    assert reasons(plan_field_update(rows, 'tasks', None, 'x')) == ['该字段不允许批量修改'] * 2


def test_plan_field_update_skips_foreign_module_and_archived_records():
    rows = [rec('tasks', {'名称': 'A', '负责人': '张三'}, 'T-1'),
            rec('projects', {'名称': 'B', '项目负责人': '张三'}, 'P-1'),
            rec('tasks', {'名称': 'C', '负责人': '张三'}, 'T-2', archived=1)]
    plan = plan_field_update(rows, 'tasks', '负责人', '李四')
    assert changed_ids(plan) == ['T-1']
    assert skipped_ids(plan) == ['P-1', 'T-2']
    assert reasons(plan) == ['记录模块不符', '记录已归档']


def test_plan_field_update_skips_unchanged_values():
    rows = [rec('tasks', {'名称': 'A', '负责人': '张三'}, 'T-1'),
            rec('tasks', {'名称': 'B', '负责人': '李四'}, 'T-2')]
    plan = plan_field_update(rows, 'tasks', '负责人', '张三')
    assert changed_ids(plan) == ['T-2']
    assert reasons(plan) == ['值未发生变化']
    assert skipped_ids(plan) == ['T-1']
    # An absent field compared with '' is also "no change".
    blank = plan_field_update([rec('tasks', {'名称': 'A'}, 'T-9')], 'tasks', '下一步', '')
    assert blank['changes'] == [] and reasons(blank) == ['值未发生变化']


def test_plan_field_update_rejects_bad_date_values():
    rows = [rec('tasks', {'名称': 'A', '截止日期': '2026-01-01'}, 'T-%d' % i)
            for i in range(5)]
    for value in ('2026/02/01', '2026-02-30', '', '20260201', '2026-2-1'):
        plan = plan_field_update(rows, 'tasks', '截止日期', value)
        assert plan['changes'] == []
        assert reasons(plan) == ['日期格式必须为YYYY-MM-DD'] * 5
    good = plan_field_update(rows[:1], 'tasks', '截止日期', '2026-02-01')
    assert changed_ids(good) == ['T-0']
    assert good['changes'][0]['data']['截止日期'] == '2026-02-01'


def test_plan_field_update_rejects_bad_number_values():
    rows = [rec('projects', {'名称': 'P%d' % i, '预算': '9'}, 'PR-%d' % i) for i in range(6)]
    for value in ('abc', '-1', '1,000', '', '1e3', ' 3'):
        plan = plan_field_update(rows, 'projects', '预算', value)
        assert plan['changes'] == []
        assert reasons(plan) == ['数值必须为非负十进制文本'] * 6
    for value in ('0', '12.5', '+3'):
        plan = plan_field_update(rows[:1], 'projects', '预算', value)
        assert changed_ids(plan) == ['PR-0'] and plan['changes'][0]['data']['预算'] == value


def test_plan_field_update_rejects_non_text_and_oversized_values():
    row = rec('tasks', {'名称': 'A', '负责人': '张三'}, 'T-1')
    assert reasons(plan_field_update([row], 'tasks', '负责人', 5)) == ['值必须为文本']
    assert reasons(plan_field_update([row], 'tasks', '负责人', None)) == ['值必须为文本']
    oversized = plan_field_update([row], 'tasks', '负责人', 'x' * 50001)
    assert reasons(oversized) == ['值不能超过50000字符']
    assert plan_field_update([row], 'tasks', '负责人', 'x' * 50000)['changes']


def test_plan_field_update_requires_task_completion_evidence_and_acceptor():
    missing_evidence = rec('tasks', {'名称': 'A', '状态': '进行中', '完成证据': '', '验收人': '张三'}, 'T-1')
    missing_acceptor = rec('tasks', {'名称': 'B', '状态': '进行中', '完成证据': '日志', '验收人': ''}, 'T-2')
    ready = rec('tasks', {'名称': 'C', '状态': '进行中', '完成证据': '日志', '验收人': '张三'}, 'T-3')
    plan = plan_field_update([missing_evidence, missing_acceptor, ready], 'tasks', '状态', '已完成')
    assert changed_ids(plan) == ['T-3']
    assert reasons(plan) == ['完成任务前需先补齐完成证据与验收人'] * 2
    assert plan['changes'][0]['data']['状态'] == '已完成'
    # An acceptor supplied in the same batch edit also satisfies the rule.
    both = plan_field_update([missing_acceptor], 'tasks', '验收人', '张三')
    assert changed_ids(both) == ['T-2']
    # ...but a plain status change elsewhere in the module is unrestricted.
    assert changed_ids(plan_field_update([ready], 'tasks', '状态', '阻塞')) == ['T-3']


def test_plan_field_update_final_review_can_only_reopen():
    final = rec('reviews', {'名称': '考核A', '状态': '已定稿', '评分': '90'}, 'R-1')
    forced = plan_field_update([final], 'reviews', '状态', '已完成')
    assert forced['changes'] == [] and reasons(forced) == ['已定稿考核只能改为待复核']
    # Any other in-batch edit of an approved evaluation is refused by the store too.
    other = plan_field_update([final], 'reviews', '评分', '95')
    assert other['changes'] == [] and reasons(other) == ['已定稿考核只能改为待复核']
    reopened = plan_field_update([final], 'reviews', '状态', '待复核')
    assert reopened['changes'][0]['data'] == {'名称': '考核A', '状态': '待复核', '评分': '90'}
    assert set(reopened['changes'][0]) == {'id', 'version', 'data'}
    assert REVIEW_STATUS[-1] == '已定稿' and '待复核' in REVIEW_STATUS
    open_row = rec('reviews', {'名称': '考核B', '状态': '待评价'}, 'R-2')
    assert reasons(plan_field_update([open_row], 'reviews', '状态', '已完成')) == ['考核状态无效']
    assert changed_ids(plan_field_update([open_row], 'reviews', '状态', '待复核')) == ['R-2']


def test_plan_field_update_summary_counts_total_selection():
    rows = [rec('tasks', {'名称': 'A', '负责人': '张三'}, 'T-1'),
            rec('tasks', {'名称': 'B', '负责人': '李四'}, 'T-2'),
            rec('people', {'姓名': '王五'}, 'P-1')]
    plan = plan_field_update(rows, 'tasks', '负责人', '赵六')
    assert plan['summary'] == {'module': 'tasks', 'field': '负责人', 'value': '赵六',
                               'total': 3, 'changed': 2, 'skipped': 1}
    assert changed_ids(plan) == ['T-1', 'T-2'] and skipped_ids(plan) == ['P-1']
    assert plan_field_update([], 'tasks', '负责人', '赵六')['summary']['total'] == 0


# --- plan_postpone ---------------------------------------------------------------

def test_plan_postpone_picks_the_priority_date_field():
    cases = {
        'tasks': '截止日期', 'risks': '截止日期', 'decisions': '截止日期',
        'collection': '截止日期', 'contracts': '到期日期', 'assets': '维保到期日期',
        'access': '复核日期', 'logs': '日期', 'recovery': '演练日期', 'plans': '截止日期',
    }
    for module, field in cases.items():
        row = rec(module, {'名称': '记录', field: '2026-06-10', '状态': '进行中'}, 'X-1')
        plan = plan_postpone([row], module, 5, today=TODAY)
        assert plan['summary']['field'] == field, module
        assert plan['summary']['days'] == 5 and plan['summary']['changed'] == 1
        assert changed_ids(plan) == ['X-1']
        assert plan['changes'][0]['data'][field] == '2026-06-15'
        assert plan['changes'][0]['data']['名称'] == '记录'
        assert set(plan['changes'][0]) == {'id', 'version', 'data'}
    assert POSTPONE_FIELDS[0] == '截止日期'
    # contracts also has 通知截止日期; exact priority must win over it.
    contract = rec('contracts', {'名称': '合同', '到期日期': '2026-06-10',
                                 '通知截止日期': '2026-06-20'}, 'C-1')
    result = plan_postpone([contract], 'contracts', 5, today=TODAY)
    assert result['changes'][0]['data']['到期日期'] == '2026-06-15'
    assert result['changes'][0]['data']['通知截止日期'] == '2026-06-20'


def test_plan_postpone_skips_modules_without_any_date_field():
    for module in ('people', 'reviews', 'systems', 'requests', 'budget'):
        row = rec(module, dict.fromkeys(MODULES[module][1], ''), 'X-1')
        plan = plan_postpone([row], module, 3, today=TODAY)
        assert plan['changes'] == [], module
        assert reasons(plan) == ['该模块没有可顺延的日期字段']
        assert plan['summary']['field'] is None
    incidents = rec('incidents', {'名称': '事件', '受理时间': '2026-06-10 09:00',
                                  '恢复时间': '2026-06-10 10:00'}, 'I-1')
    assert reasons(plan_postpone([incidents], 'incidents', 3, today=TODAY)) == ['该模块没有可顺延的日期字段']


def test_plan_postpone_accepts_day_bounds():
    row = rec('tasks', {'名称': 'A', '截止日期': '2026-06-10'}, 'T-1')
    assert plan_postpone([row], 'tasks', 1, today=TODAY)['changes'][0]['data']['截止日期'] == '2026-06-11'
    assert plan_postpone([row], 'tasks', 3650, today=TODAY)['changes'][0]['data']['截止日期'] == '2036-06-07'


@pytest.mark.parametrize('days', [0, -1, 3651, 4000, True, False, '3', 3.0, None])
def test_plan_postpone_rejects_invalid_days(days):
    row = rec('tasks', {'名称': 'A', '截止日期': '2026-06-10'}, 'T-1')
    with pytest.raises(ValueError, match='批量顺延天数无效'):
        plan_postpone([row], 'tasks', days, today=TODAY)
    with pytest.raises(ValueError, match='批量顺延天数无效'):
        plan_postpone([row], 'tasks', days, new_date='2026-07-01', today=TODAY)


def test_plan_postpone_fixed_date_and_past_refusal():
    row = rec('tasks', {'名称': 'A', '截止日期': '2026-01-01'}, 'T-1')
    fixed = plan_postpone([row], 'tasks', 30, new_date='2026-07-01', today=TODAY)
    assert fixed['changes'][0]['data']['截止日期'] == '2026-07-01'
    assert fixed['summary']['new_date'] == '2026-07-01' and fixed['summary']['today'] == '2026-06-01'
    for new_date in ('2026-05-31', '2026-06-01T00:00:00'):
        if new_date == '2026-05-31':
            past = plan_postpone([row], 'tasks', 30, new_date=new_date, today=TODAY)
            assert past['changes'] == []
            assert reasons(past) == ['顺延后的日期不能早于今天']
        else:
            assert plan_postpone([row], 'tasks', 30, new_date=new_date, today=TODAY)['changes']
    arithmetic = plan_postpone([row], 'tasks', 5, today=TODAY)
    assert reasons(arithmetic) == ['顺延后的日期不能早于今天']
    for bad in ('2026/07/01', '2026-13-01', '', 'x'):
        with pytest.raises(ValueError, match='批量顺延日期无效'):
            plan_postpone([row], 'tasks', 1, new_date=bad, today=TODAY)
    with pytest.raises(ValueError, match='批量顺延日期无效'):
        plan_postpone([row], 'tasks', 1, today='not-a-day')


def test_plan_postpone_allows_today_as_the_result():
    row = rec('tasks', {'名称': 'A', '截止日期': '2026-05-27'}, 'T-1')
    plan = plan_postpone([row], 'tasks', 5, today=TODAY)
    assert changed_ids(plan) == ['T-1']
    assert plan['changes'][0]['data']['截止日期'] == '2026-06-01'


def test_plan_postpone_skips_unusable_and_closed_records():
    rows = [rec('tasks', {'名称': '空日期', '截止日期': '', '状态': '进行中'}, 'T-1'),
            rec('tasks', {'名称': '非日期', '截止日期': '2026-06-10 09:00', '状态': '进行中'}, 'T-2'),
            rec('tasks', {'名称': '坏日期', '截止日期': '2026-13-40', '状态': '进行中'}, 'T-3'),
            rec('tasks', {'名称': '已完成', '截止日期': '2026-06-10', '状态': '已完成'}, 'T-4'),
            rec('tasks', {'名称': '取消', '截止日期': '2026-06-10', '状态': '已取消'}, 'T-5'),
            rec('tasks', {'名称': '归档', '截止日期': '2026-06-10', '状态': '进行中'}, 'T-6', archived=1),
            rec('projects', {'名称': '别模块', '截止日期': '2026-06-10', '状态': '进行中'}, 'P-1')]
    plan = plan_postpone(rows, 'tasks', 5, today=TODAY)
    assert plan['changes'] == []
    assert reasons(plan) == ['原日期无效，请先人工填写', '原日期无效，请先人工填写', '原日期无效，请先人工填写',
                             '已结束记录不参与顺延', '已结束记录不参与顺延', '记录已归档', '记录模块不符']
    assert skipped_ids(plan) == ['T-1', 'T-2', 'T-3', 'T-4', 'T-5', 'T-6', 'P-1']
    assert ENDED_STATUSES == frozenset({'已完成', '已取消', '已关闭', '已定稿'})
    assert plan['summary'] == {'module': 'tasks', 'field': '截止日期', 'days': 5,
                               'new_date': None, 'today': '2026-06-01',
                               'total': 7, 'changed': 0, 'skipped': 7}


# --- plan_archive ----------------------------------------------------------------

def test_plan_archive_targets_and_skipped_records():
    rows = [rec('tasks', {'名称': 'A'}, 'T-1', version=3),
            rec('tasks', {'名称': 'B'}, 'T-2', version=1, archived=1),
            rec('tasks', {'名称': 'C'}, 'T-1', version=3),
            {'module': 'tasks', 'data': {'名称': 'D'}}]
    plan = plan_archive(rows)
    assert set(plan) == {'targets', 'skipped'}
    assert plan['targets'] == [{'id': 'T-1', 'version': 3}]
    assert set(plan['targets'][0]) == {'id', 'version'}  # exact keys of batch-archive
    assert plan['skipped'] == [{'id': 'T-2', 'reason': '记录已归档'},
                               {'id': 'T-1', 'reason': '记录重复选择'},
                               {'id': None, 'reason': '记录ID无效'}]
    assert plan_archive([]) == {'targets': [], 'skipped': []}
    assert plan_archive(rec('tasks', {'名称': '单个'}, 'T-7', version=2))['targets'] == [
        {'id': 'T-7', 'version': 2}]


# --- export_rows -----------------------------------------------------------------

def test_export_rows_headers_rows_and_string_cells():
    multi_line = '第一行,含逗号\n第二行'
    rows = [rec('tasks', {'名称': '任务A', '截止日期': '2026-06-10', '项目ID': None,
                          '下一步': multi_line, '风险与依赖': 7}, 'T-1'),
            rec('tasks', {'名称': '任务B'}, 'T-2')]
    result = export_rows(rows, 'tasks')
    assert set(result) == {'headers', 'rows', 'module', 'generated'}
    assert result['module'] == 'tasks'
    assert result['headers'] == ['记录ID'] + MODULES['tasks'][1]
    assert len(result['rows']) == 2
    first, second = result['rows']
    assert first[0] == 'T-1' and second[0] == 'T-2'
    assert all(isinstance(cell, str) for row in result['rows'] for cell in row)
    assert len(first) == len(result['headers']) == len(second)
    assert first[result['headers'].index('截止日期')] == '2026-06-10'
    assert first[result['headers'].index('项目ID')] == ''
    assert first[result['headers'].index('下一步')] == multi_line
    assert first[result['headers'].index('风险与依赖')] == '7'
    assert second[result['headers'].index('截止日期')] == ''


def test_export_rows_columns_subset_keeps_schema_order():
    rows = [rec('tasks', {'名称': 'A', '负责人': '张三', '状态': '进行中'}, 'T-1')]
    result = export_rows(rows, 'tasks', columns=['状态', '负责人', '没有这个字段'])
    assert result['headers'] == ['记录ID', '负责人', '状态']
    assert result['rows'] == [['T-1', '张三', '进行中']]
    assert export_rows(rows, 'tasks', columns=[])['headers'] == ['记录ID']


def test_export_rows_skips_records_from_other_modules():
    rows = [rec('tasks', {'名称': 'A'}, 'T-1'),
            rec('projects', {'名称': 'P'}, 'P-1'),
            {'id': 'T-9', 'data': {'名称': '无模块'}}]
    result = export_rows(rows, 'tasks')
    assert [row[0] for row in result['rows']] == ['T-1', 'T-9']


def test_export_rows_marks_generated_with_an_iso_timestamp():
    result = export_rows([], 'logs')
    assert result['rows'] == []
    assert result['headers'] == ['记录ID'] + MODULES['logs'][1]
    assert datetime.fromisoformat(result['generated']).tzinfo is not None


# --- to_csv ----------------------------------------------------------------------

def test_to_csv_escapes_commas_quotes_and_newlines():
    text = to_csv(['记录ID', '名称'], [['T-1', '含,逗号'], ['T-2', '含"引号"'], ['T-3', '两行\n内容']])
    assert text.startswith('记录ID,名称\r\n')
    assert text.endswith('\r\n') and text.count('\r\n') == 4
    assert '"含,逗号"' in text
    assert '"含""引号"""' in text
    assert '"两行\n内容"' in text
    assert list(csv.reader(io.StringIO(text))) == [
        ['记录ID', '名称'], ['T-1', '含,逗号'], ['T-2', '含"引号"'], ['T-3', '两行\n内容']]


def test_to_csv_prefixes_formula_and_control_cells():
    text = to_csv(['记录ID', '值'], [['T-1', '=1+1'], ['T-2', '+1'], ['T-3', '-1'],
                                     ['T-4', '@x'], ['T-5', '\tTab'], ['T-6', '\rCR']])
    assert text == ('记录ID,值\r\n'
                    "T-1,'=1+1\r\n"
                    "T-2,'+1\r\n"
                    "T-3,'-1\r\n"
                    "T-4,'@x\r\n"
                    "T-5,'\tTab\r\n"
                    "T-6,\"'\rCR\"\r\n")
    plain = to_csv(['记录ID', '值'], [['T-7', '2026-06-10'], ['T-8', ''], ['T-9', '安全文本']])
    assert plain == '记录ID,值\r\nT-7,2026-06-10\r\nT-8,\r\nT-9,安全文本\r\n'


def test_to_csv_empty_input_and_header_only():
    assert to_csv([], []) == ''
    assert to_csv(None, None) == ''
    assert to_csv(['记录ID'], []) == '记录ID\r\n'


# --- safe_filename ---------------------------------------------------------------

def test_safe_filename_shape_and_sanitising():
    assert safe_filename('tasks', date(2026, 10, 10)) == 'IT台账_任务管理_2026-10-10.csv'
    assert safe_filename('tasks', '2026-10-10') == 'IT台账_任务管理_2026-10-10.csv'
    assert safe_filename('tasks', datetime(2026, 10, 10, 9, 30)) == 'IT台账_任务管理_2026-10-10.csv'
    dirty = safe_filename('a/b:c*?|<>"\\d', '2026-10-10')
    assert dirty == 'IT台账_abcd_2026-10-10.csv'
    assert not any(char in dirty for char in '/\\:*?"<>|')
    assert safe_filename('tasks', None) == 'IT台账_任务管理.csv'
    assert safe_filename('', None) == 'IT台账_导出.csv'


def test_safe_filename_is_bounded_and_keeps_extension_and_stamp():
    long_ascii = safe_filename('x' * 400, date(2026, 10, 10))
    assert len(long_ascii) == 120 and long_ascii.endswith('.csv') and long_ascii.startswith('IT台账_')
    assert '2026-10-10' in long_ascii
    long_cjk = safe_filename('任务' * 200, date(2026, 10, 10))
    assert len(long_cjk) == 120 and long_cjk.endswith('.csv') and '2026-10-10' in long_cjk
    assert len(safe_filename('tasks', '2' * 500)) <= 120
    assert safe_filename('tasks', '2' * 500).endswith('.csv')


# --- cross-cutting invariants ----------------------------------------------------

def test_safe_batch_errors_is_a_frozenset_with_the_fixed_messages():
    assert isinstance(SAFE_BATCH_ERRORS, frozenset)
    assert {'批量操作模块无效', '批量顺延天数无效', '批量导出模块无效', '记录模块不符', '记录已归档',
            '该字段不允许批量修改', '值未发生变化', '日期格式必须为YYYY-MM-DD',
            '数值必须为非负十进制文本', '完成任务前需先补齐完成证据与验收人',
            '已定稿考核只能改为待复核', '考核状态无效', '该模块没有可顺延的日期字段',
            '原日期无效，请先人工填写', '已结束记录不参与顺延', '顺延后的日期不能早于今天'} <= SAFE_BATCH_ERRORS


def test_every_skip_reason_comes_from_the_safe_constants():
    rows = [rec('tasks', {'名称': 'A', '负责人': '张三', '截止日期': '', '状态': '进行中'}, 'T-1'),
            rec('tasks', {'名称': 'B', '负责人': '张三', '截止日期': '2026-06-10', '状态': '已完成'}, 'T-2'),
            rec('tasks', {'名称': 'C', '负责人': '张三', '截止日期': '2026-06-10', '状态': '进行中'}, 'T-3',
                archived=1),
            rec('projects', {'名称': 'D', '预算': 'x'}, 'P-1'),
            rec('reviews', {'名称': 'E', '状态': '已定稿'}, 'R-1')]
    plans = [plan_field_update(rows, 'tasks', '负责人', '张三'),
             plan_field_update(rows, 'tasks', '完成证据', 'x'),
             plan_field_update(rows, 'tasks', '状态', '已完成'),
             plan_field_update(rows, 'reviews', '状态', '待评价'),
             plan_postpone(rows, 'tasks', 5, today=TODAY),
             plan_postpone(rows, 'tasks', 5, new_date='2026-05-01', today=TODAY),
             plan_postpone(rows, 'reviews', 5, today=TODAY),
             plan_archive(rows + [rows[0]])]
    collected = [reason for plan in plans for reason in reasons(plan)]
    assert collected, 'fixtures must trigger refusals'
    assert set(collected) <= SAFE_BATCH_ERRORS
    assert len(collected) == sum(len(plan['skipped']) for plan in plans)


def test_plans_never_modify_the_supplied_records():
    rows = [rec('tasks', {'名称': '任务A', '负责人': '张三', '截止日期': '2026-06-10',
                          '状态': '进行中'}, 'T-1'),
            rec('tasks', {'名称': '任务B', '负责人': '李四', '截止日期': '',
                          '状态': '已完成'}, 'T-2'),
            rec('tasks', {'名称': '任务C'}, 'T-3', archived=1),
            rec('people', {'姓名': '王五'}, 'P-1')]
    snapshot = copy.deepcopy(rows)
    update = plan_field_update(rows, 'tasks', '负责人', '赵六')
    postpone = plan_postpone(rows, 'tasks', 5, today=TODAY)
    plan_field_update(rows, 'tasks', '验收人', '钱七')
    plan_archive(rows)
    export_rows(rows, 'tasks')
    plan_postpone(rows, 'people', 5, today=TODAY)
    assert rows == snapshot
    assert update['changes'] and postpone['changes']
    for plan in (update, postpone):
        for change in plan['changes']:
            assert change['data'] is not rows[0]['data']
    update['changes'][0]['data']['负责人'] = '被篡改'
    postpone['changes'][0]['data']['截止日期'] = '1999-01-01'
    export_rows(rows, 'tasks')['rows'][0][0] = '被篡改'
    assert rows == snapshot
    assert rows[0]['data']['负责人'] == '张三'
    assert rows[0]['data']['截止日期'] == '2026-06-10'
