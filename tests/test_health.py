"""Data-health checks: read-only, offline, deterministic; all fixtures are fictional."""
import json
from collections import Counter
from datetime import date, datetime, timedelta

import pytest

from itmanager import health

TODAY = date(2026, 6, 1)
RECENT = '2026-05-25T00:00:00+00:00'
CODES = {'missing_owner', 'missing_date', 'missing_evidence', 'duplicate_number',
         'overdue_unhandled', 'orphan_reference', 'review_overdue', 'stale_open',
         'system_no_recovery', 'review_missing_cycle'}


def rec(module, data, oid, updated=RECENT, archived=0):
    return {'id': oid, 'module': module, 'data': data, 'version': 1,
            'updated': updated, 'archived': archived}


def old(days):
    return (TODAY - timedelta(days=days)).isoformat() + 'T00:00:00+00:00'


def soon(days):
    return (TODAY + timedelta(days=days)).isoformat()


def scan(rows):
    return health.scan(rows, TODAY)


def codes(findings):
    return sorted(item['code'] for item in findings)


def for_record(findings, oid):
    return [item for item in findings if item['record_id'] == oid]


def clean_task(oid='T-1'):
    return rec('tasks', {'名称': '按期任务', '负责人': '张三', '截止日期': soon(10),
                         '状态': '未开始'}, oid)


def test_clean_records_produce_no_findings():
    task = clean_task()
    person = rec('people', {'姓名': '张三', '部门': 'IT', '岗位': '工程师', '主管': '李四',
                            '状态': '进行中'}, 'P-1')
    project = rec('projects', {'名称': '项目A', '业务负责人': '李四', '项目负责人': '张三',
                               '截止日期': soon(30), '状态': '进行中'}, 'PR-1')
    result = scan([task, person, project])
    assert result['findings'] == []
    assert result['scanned'] == 3
    assert result['summary'] == {'total': 0, 'high': 0, 'medium': 0, 'low': 0,
                                 'by_module': {}, 'by_code': {}}
    assert isinstance(result['generated'], str) and result['generated']


def test_missing_owner_reports_each_applicable_field():
    rows = [rec('tasks', {'名称': '无人负责', '负责人': '', '截止日期': soon(10),
                          '状态': '未开始'}, 'T-1'),
            rec('systems', {'名称': 'ERP', '业务负责人': '张三', '技术负责人': '',
                            '关键性': '高', 'RTO小时': '4', 'RPO小时': '1',
                            '证据': '台账截图'}, 'S-1')]
    findings = scan(rows)['findings']
    assert codes(for_record(findings, 'T-1')) == ['missing_owner']
    assert for_record(findings, 'T-1')[0]['field'] == '负责人'
    assert for_record(findings, 'T-1')[0]['severity'] == '中'
    assert codes(for_record(findings, 'S-1')) == ['missing_owner']
    assert for_record(findings, 'S-1')[0]['field'] == '技术负责人'


def test_missing_date_reports_key_dates_only():
    row = rec('contracts', {'名称': '缺日期合同', '供应商': '供应商A', '负责人': '李四',
                            '到期日期': '', '状态': '进行中', '证据': '扫描件'}, 'C-1')
    findings = scan([row])['findings']
    assert [(item['code'], item['field']) for item in findings] == [('missing_date', '到期日期')]
    assert findings[0]['severity'] == '中'
    scheduled = rec('tasks', {'名称': '有日期', '负责人': '张三', '截止日期': soon(5),
                              '状态': '未开始'}, 'T-1')
    assert scan([scheduled])['findings'] == []


def test_missing_evidence_skips_initial_statuses():
    executing = rec('tasks', {'名称': '执行中', '负责人': '张三', '截止日期': soon(5),
                              '状态': '进行中', '完成证据': ''}, 'T-1')
    initial = rec('tasks', {'名称': '未开始', '负责人': '张三', '截止日期': soon(5),
                            '状态': '未开始', '完成证据': ''}, 'T-2')
    review = rec('reviews', {'名称': '考核A', '复核人': '主管', '考核周期': '2026Q1',
                             '指标口径': '按交付', '成果证据': '', '状态': '目标设定'}, 'R-1')
    findings = scan([executing, initial, review])['findings']
    evidence = [item for item in findings if item['code'] == 'missing_evidence']
    assert [(item['record_id'], item['field']) for item in evidence] == [('T-1', '完成证据')]
    assert for_record(findings, 'T-2') == []
    assert for_record(findings, 'R-1') == []


def test_duplicate_number_ignores_empty_values_and_archived_records():
    rows = [rec('assets', {'名称': '笔记本', '资产编号': 'A-001', '责任人': '张三'}, 'A-1'),
            rec('assets', {'名称': '同号资产', '资产编号': 'A-001', '责任人': '李四'}, 'A-2'),
            rec('assets', {'名称': '空号', '资产编号': '', '责任人': '王五'}, 'A-3'),
            rec('assets', {'名称': '空号二', '资产编号': '   ', '责任人': '王五'}, 'A-4'),
            rec('assets', {'名称': '归档同号', '资产编号': 'A-001', '责任人': '赵六'},
                'A-5', archived=1)]
    duplicates = [item for item in scan(rows)['findings'] if item['code'] == 'duplicate_number']
    assert sorted(item['record_id'] for item in duplicates) == ['A-1', 'A-2']
    assert all(item['field'] == '资产编号' for item in duplicates)
    assert all('另外 1 条' in item['detail'] for item in duplicates)
    systems = [rec('systems', {'名称': 'ERP', '关键性': '高', 'RTO小时': '4', 'RPO小时': '1'},
                   'S-1'),
               rec('systems', {'名称': 'ERP', '关键性': '高', 'RTO小时': '4', 'RPO小时': '1'},
                   'S-2'),
               rec('systems', {'名称': '归档ERP', '关键性': '高', 'RTO小时': '4',
                               'RPO小时': '1'}, 'S-3', archived=1)]
    dup2 = [item for item in scan(systems)['findings'] if item['code'] == 'duplicate_number']
    assert sorted(item['record_id'] for item in dup2) == ['S-1', 'S-2']


def test_overdue_unhandled_needs_past_due_and_no_open_link():
    parent = rec('tasks', {'名称': '逾期父任务', '负责人': '张三', '截止日期': soon(-5),
                           '状态': '进行中', '完成证据': '阶段记录'}, 'T-1')
    findings = scan([parent])['findings']
    assert codes(for_record(findings, 'T-1')) == ['overdue_unhandled']
    assert for_record(findings, 'T-1')[0]['severity'] == '高'
    assert for_record(findings, 'T-1')[0]['field'] == '截止日期'
    child = rec('tasks', {'名称': '子任务', '负责人': '张三', '父任务ID': 'T-1',
                          '截止日期': soon(20), '状态': '进行中', '完成证据': '阶段记录'}, 'T-2')
    linked = scan([parent, child])['findings']
    assert [item for item in linked if item['code'] == 'overdue_unhandled'] == []
    done_child = rec('tasks', {'名称': '已完成子任务', '负责人': '张三', '父任务ID': 'T-1',
                               '截止日期': soon(20), '状态': '已完成', '完成证据': '验收单'}, 'T-3')
    unlinked = scan([parent, done_child])['findings']
    assert codes(for_record(unlinked, 'T-1')) == ['overdue_unhandled']
    finished = rec('tasks', {'名称': '已完成父任务', '负责人': '张三', '截止日期': soon(-5),
                             '状态': '已完成', '完成证据': '验收单'}, 'T-4')
    assert scan([finished])['findings'] == []


def test_orphan_reference_detects_missing_archived_and_wrong_module():
    live = rec('projects', {'名称': '项目A', '项目负责人': '张三', '截止日期': soon(30),
                            '状态': '进行中'}, 'P-1')
    archived = rec('projects', {'名称': '归档项目', '项目负责人': '张三',
                                '截止日期': soon(30), '状态': '进行中'}, 'P-2', archived=1)
    rows = [live, archived,
            rec('tasks', {'名称': '悬空任务', '负责人': '张三', '项目ID': 'P-404',
                          '截止日期': soon(5), '状态': '进行中', '完成证据': '记录'}, 'T-1'),
            rec('tasks', {'名称': '归档引用', '负责人': '张三', '项目ID': 'P-2',
                          '截止日期': soon(5), '状态': '进行中', '完成证据': '记录'}, 'T-2'),
            rec('tasks', {'名称': '正常引用', '负责人': '张三', '项目ID': 'P-1',
                          '截止日期': soon(5), '状态': '进行中', '完成证据': '记录'}, 'T-3'),
            rec('logs', {'名称': '工作记录', '日期': '2026-05-25', '工作内容': '巡检',
                         '证据': '截图'}, 'L-1'),
            rec('tasks', {'名称': '模块不符', '负责人': '张三', '项目ID': 'L-1',
                          '截止日期': soon(5), '状态': '进行中', '完成证据': '记录'}, 'T-4')]
    orphan = {item['record_id']: item for item in scan(rows)['findings']
              if item['code'] == 'orphan_reference'}
    assert set(orphan) == {'T-1', 'T-2', 'T-4'}
    assert all(item['field'] == '项目ID' for item in orphan.values())
    assert '不存在' in orphan['T-1']['detail']
    assert '已归档' in orphan['T-2']['detail']
    assert '模块不符' in orphan['T-4']['detail']
    assert for_record(scan(rows)['findings'], 'T-3') == []


def test_review_overdue_for_access_and_risks():
    rows = [rec('access', {'名称': '权限复核A', '责任人': '张三', '复核日期': '2026-05-01',
                           '状态': '进行中'}, 'AC-1'),
            rec('risks', {'名称': '风险A', '负责人': '张三', '复核日期': '2026-05-01',
                          '状态': '待整改'}, 'RK-1'),
            rec('access', {'名称': '已关闭复核', '责任人': '张三', '复核日期': '2026-05-01',
                           '状态': '已关闭'}, 'AC-2'),
            rec('access', {'名称': '未来复核', '责任人': '张三', '复核日期': soon(30),
                           '状态': '进行中'}, 'AC-3')]
    findings = scan(rows)['findings']
    overdue = {item['record_id'] for item in findings if item['code'] == 'review_overdue'}
    assert overdue == {'AC-1', 'RK-1'}
    assert all(item['field'] == '复核日期' for item in findings if item['code'] == 'review_overdue')
    assert for_record(findings, 'AC-3') == []


def test_stale_open_before_and_after_180_days():
    def task(age, oid, status='进行中'):
        return rec('tasks', {'名称': '任务' + oid, '负责人': '张三', '截止日期': soon(30),
                             '状态': status, '完成证据': '记录'}, oid, updated=old(age))
    findings = scan([task(180, 'T-1'), task(181, 'T-2')])['findings']
    stale = [item for item in findings if item['code'] == 'stale_open']
    assert [item['record_id'] for item in stale] == ['T-2']
    assert stale[0]['severity'] == '低' and stale[0]['field'] == 'updated'
    assert [item for item in scan([task(400, 'T-3', '已完成')])['findings']
            if item['code'] == 'stale_open'] == []


def test_system_no_recovery_flags_each_missing_field():
    rows = [rec('systems', {'名称': 'ERP', '业务负责人': '张三', '技术负责人': '李四',
                            '关键性': '高', 'RTO小时': '4', 'RPO小时': '1',
                            '证据': '台账'}, 'S-1'),
            rec('systems', {'名称': 'CRM', '业务负责人': '张三', '技术负责人': '李四',
                            '关键性': '', 'RTO小时': '', 'RPO小时': '2',
                            '证据': '台账'}, 'S-2')]
    findings = scan(rows)['findings']
    assert for_record(findings, 'S-1') == []
    missing = for_record(findings, 'S-2')
    assert {item['field'] for item in missing} == {'关键性', 'RTO小时'}
    assert all(item['code'] == 'system_no_recovery' and item['severity'] == '中'
               for item in missing)


def test_review_missing_cycle_reports_cycle_and_metric():
    rows = [rec('people', {'姓名': '张三', '主管': '李四', '状态': '进行中'}, 'P-1'),
            rec('reviews', {'名称': '考核A', '人员ID': 'P-1', '考核周期': '2026Q1',
                            '指标口径': '按交付', '复核人': '李四', '成果证据': '已归档',
                            '状态': '待评价'}, 'R-1'),
            rec('reviews', {'名称': '考核B', '复核人': '李四', '考核周期': '',
                            '指标口径': '', '成果证据': '已归档', '状态': '待评价'}, 'R-2')]
    findings = scan(rows)['findings']
    assert for_record(findings, 'R-1') == []
    missing = for_record(findings, 'R-2')
    assert {item['field'] for item in missing} == {'考核周期', '指标口径'}
    assert all(item['code'] == 'review_missing_cycle' and item['severity'] == '低'
               for item in missing)


def test_every_documented_code_can_fire():
    rows = [
        rec('tasks', {'名称': '无负责人', '负责人': '', '截止日期': soon(5),
                      '状态': '未开始'}, 'T-OWNER'),
        rec('contracts', {'名称': '缺日期', '负责人': '张三', '到期日期': '',
                          '状态': '进行中', '证据': '扫描件'}, 'C-DATE'),
        rec('tasks', {'名称': '缺证据', '负责人': '张三', '截止日期': soon(5),
                      '状态': '进行中', '完成证据': ''}, 'T-EVIDENCE'),
        rec('assets', {'名称': '资产一', '资产编号': 'DUP-1', '责任人': '张三',
                       '维保到期日期': soon(30)}, 'A-DUP1'),
        rec('assets', {'名称': '资产二', '资产编号': 'DUP-1', '责任人': '李四',
                       '维保到期日期': soon(30)}, 'A-DUP2'),
        rec('tasks', {'名称': '逾期未处理', '负责人': '张三', '截止日期': soon(-5),
                      '状态': '进行中', '完成证据': '记录'}, 'T-OVERDUE'),
        rec('tasks', {'名称': '悬空引用', '负责人': '张三', '项目ID': 'P-404',
                      '截止日期': soon(5), '状态': '进行中', '完成证据': '记录'}, 'T-ORPHAN'),
        rec('access', {'名称': '复核超期', '责任人': '张三', '复核日期': soon(-5),
                       '状态': '进行中'}, 'AC-REVIEW'),
        rec('tasks', {'名称': '长期未更新', '负责人': '张三', '截止日期': soon(5),
                      '状态': '进行中', '完成证据': '记录'}, 'T-STALE', updated=old(400)),
        rec('systems', {'名称': '无恢复目标', '业务负责人': '张三', '技术负责人': '李四',
                        '关键性': '', 'RTO小时': '', 'RPO小时': ''}, 'S-RECOVERY'),
        rec('reviews', {'名称': '缺周期', '复核人': '李四', '考核周期': '',
                        '指标口径': '', '成果证据': '已归档', '状态': '待评价'}, 'R-CYCLE'),
    ]
    fired = {item['code'] for item in scan(rows)['findings']}
    assert CODES <= fired
    assert fired <= CODES
    assert all(code in CODES for code in fired)


def test_finding_shape_severity_and_deterministic_sort():
    rows = [rec('tasks', {'名称': '逾期', '负责人': '张三', '截止日期': soon(-5),
                          '状态': '进行中', '完成证据': ''}, 'T-1'),
            rec('assets', {'名称': '资产', '资产编号': '', '责任人': '',
                           '维保到期日期': '', '状态': '进行中'}, 'A-1'),
            rec('systems', {'名称': '系统', '业务负责人': '', '技术负责人': '',
                            '关键性': '', 'RTO小时': '', 'RPO小时': ''}, 'S-1')]
    findings = scan(rows)['findings']
    assert findings
    for item in findings:
        assert list(item) == ['code', 'severity', 'module', 'record_id', 'title', 'field',
                              'detail', 'suggestion']
        assert item['severity'] in {'高', '中', '低'}
        assert item['code'] in CODES and item['title'] and item['detail'] and item['suggestion']
    rank = {'高': 0, '中': 1, '低': 2}
    assert findings == sorted(findings, key=lambda item: (rank[item['severity']], item['module'],
                                                          item['record_id'], item['code'],
                                                          item['field']))
    assert scan(rows)['findings'] == findings
    assert json.dumps(findings, ensure_ascii=False)


def test_scan_validates_inputs_and_skips_unusable_rows():
    with pytest.raises(ValueError) as excinfo:
        health.scan('not-a-list', TODAY)
    assert str(excinfo.value) == '体检输入记录格式无效'
    with pytest.raises(ValueError) as excinfo:
        health.scan([1, 2], TODAY)
    assert str(excinfo.value) == '体检输入记录格式无效'
    for bad_today in ('2026-06-01', datetime(2026, 6, 1, 9, 0), 20260601):
        with pytest.raises(ValueError) as excinfo:
            health.scan([], bad_today)
        assert str(excinfo.value) == '体检扫描日期无效'
    assert health.scan([], None)['findings'] == []
    skipped = health.scan([{'id': 'X-1', 'module': 'unknown', 'data': {}},
                           {'id': '', 'module': 'tasks', 'data': {}},
                           {'module': 'tasks', 'data': {}}], TODAY)
    assert skipped['scanned'] == 0 and skipped['findings'] == []
    for message in ('体检输入记录格式无效', '体检扫描日期无效', '体检整改项数量无效'):
        assert message in health.SAFE_HEALTH_ERRORS


def test_archived_records_are_not_scanned():
    broken = rec('tasks', {'名称': '归档坏任务', '截止日期': '2020-01-01', '状态': '进行中'},
                 'T-1', updated='2020-01-01T00:00:00+00:00', archived=1)
    result = scan([broken])
    assert result['scanned'] == 0 and result['findings'] == []


def test_summarize_counts_by_severity_module_and_code():
    rows = [rec('tasks', {'名称': '逾期', '负责人': '张三', '截止日期': soon(-5),
                          '状态': '进行中', '完成证据': ''}, 'T-1'),
            rec('systems', {'名称': '系统', '业务负责人': '', '技术负责人': '',
                            '关键性': '', 'RTO小时': '', 'RPO小时': ''}, 'S-1')]
    findings = scan(rows)['findings']
    summary = health.summarize(findings)
    assert summary['total'] == len(findings)
    assert summary['high'] + summary['medium'] + summary['low'] == len(findings)
    assert summary['by_code'] == dict(Counter(item['code'] for item in findings))
    assert summary['by_module'] == dict(Counter(item['module'] for item in findings))
    assert health.summarize([]) == {'total': 0, 'high': 0, 'medium': 0, 'low': 0,
                                    'by_module': {}, 'by_code': {}}


def test_finding_task_items_shape_order_and_empty_input():
    rows = [rec('tasks', {'名称': '逾期任务', '负责人': '张三', '截止日期': soon(-5),
                          '状态': '进行中', '完成证据': ''}, 'TASKS-abc123456789')]
    findings = scan(rows)['findings']
    assert findings
    items = health.finding_task_items(findings)
    assert len(items) == len(findings)
    for item, finding in zip(items, findings):
        assert list(item) == ['name', 'owner', 'due_date', 'next_step', 'acceptance']
        assert item['acceptance'] == '整改完成并留存证据'
        assert item['due_date'] == (date.today() + timedelta(days=14)).isoformat()
        assert item['owner'] == ''
        assert item['name'].startswith('整改：')
        assert item['name'].endswith('（%s）' % (finding['field'] or finding['code']))
        assert finding['record_id'] not in item['name']
        assert item['next_step']
    assert health.finding_task_items(findings, limit=1) == items[:1]
    assert health.finding_task_items([]) == []
    assert health.finding_task_items([], limit=20) == []


def test_finding_task_items_validates_limit_and_input():
    with pytest.raises(ValueError) as excinfo:
        health.finding_task_items('not-a-list')
    assert str(excinfo.value) == '体检输入记录格式无效'
    for bad_limit in (0, -1, 21, '5', None, True, 1.5):
        with pytest.raises(ValueError) as excinfo:
            health.finding_task_items([], limit=bad_limit)
        assert str(excinfo.value) == '体检整改项数量无效'
    assert len(health.finding_task_items([{'title': '甲', 'field': '乙',
                                           'suggestion': '丙', 'code': '丁'}], limit=20)) == 1
