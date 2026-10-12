"""Lifecycle chain tests: pure fictional records, no database, no network."""
import pytest

from itmanager.lifecycle import (
    LINK_FIELDS, MAX_DEPTH_LIMIT, SAFE_LIFECYCLE_ERRORS, STAGE_LABELS, STAGES,
    chain, stage_progress, summarize_chain, title_of,
)


def event(eid='INC-1', **data):
    """Minimal 运维事件 record; ``根因`` is deliberately left empty by default."""
    base = {'名称': '系统不可用', '系统ID': 'SYS-1', '受理时间': '2024-03-01T09:00',
            '影响范围': '全部用户', '级别': '高', '负责人': '张三', '状态': '进行中',
            '处置过程': '回滚', '恢复时间': '2024-03-01T11:00', '业务确认': '',
            '根因': '', '后续任务ID': ''}
    base.update(data)
    return {'id': eid, 'module': 'incidents', 'data': base}


def risk(rid='RSK-1', **data):
    base = {'名称': '配置漂移风险', '关联ID': 'INC-1', '风险类别': '可用性', '影响': '高',
            '等级': '高', '负责人': '李四', '临时措施': '人工回滚', '长期措施': '配置基线',
            '截止日期': '2024-03-10', '状态': '进行中', '残余风险': '中', '批准人': '王五',
            '复核日期': '2024-03-20', '关闭证据': '', '整改任务ID': 'TSK-1'}
    base.update(data)
    return {'id': rid, 'module': 'risks', 'data': base}


def change(cid='CHG-1', **data):
    base = {'名称': '调整配置基线', '关联ID': 'RSK-1', '系统ID': 'SYS-1', '执行人': '李四',
            '计划窗口': '2024-03-05T20:00', '影响范围': '核心系统', '测试证据': '测试报告',
            '备份': '已完成', '回退判据及步骤': '异常即回滚', '批准人': '王五',
            '状态': '进行中', '验证证据': '监控截图'}
    base.update(data)
    return {'id': cid, 'module': 'changes', 'data': base}


def task(tid='TSK-1', **data):
    base = {'名称': '落地配置基线', '项目ID': 'PRJ-1', '父任务ID': '', '负责人': '李四',
            '优先级': '高', '开始日期': '2024-03-06', '截止日期': '2024-03-09',
            '状态': '进行中', '下一步': '编写基线脚本', '阻塞原因': '', '验收人': '王五',
            '完成证据': '', '整改任务ID': '', '后续任务ID': ''}
    base.update(data)
    return {'id': tid, 'module': 'tasks', 'data': base}

def log(lid='LOG-1', **data):
    base = {'名称': '复盘会议', '日期': '2024-03-12', '人员ID': 'PPL-1', '关联ID': 'INC-1',
            '工作内容': '复盘', '结果': '形成整改项', '证据': '会议纪要', '下一步': '跟踪整改'}
    base.update(data)
    return {'id': lid, 'module': 'logs', 'data': base}


def recovery(rid='RCV-1', **data):
    base = {'名称': '季度恢复演练', '系统ID': 'SYS-1', '负责人': '李四', '备份范围': '全量',
            '频率及保留': '每日/30天', '隔离策略': '离线', 'RTO小时': '4', 'RPO小时': '1',
            '演练日期': '2024-03-15', '实际耗时': '3', '实际数据缺口': '0', '结论': '通过',
            '证据': '演练报告', '整改任务ID': 'TSK-1'}
    base.update(data)
    return {'id': rid, 'module': 'recovery', 'data': base}


def project(pid='PRJ-1', **data):
    base = {'名称': '稳定性专项', '规划ID': '', '业务负责人': '王五', '项目负责人': '李四',
            '阶段': '执行', '开始日期': '2024-03-01', '截止日期': '2024-06-30',
            '状态': '进行中', '预算': '100000', '验收标准': '可用性达标', '风险': '',
            '收益复盘': ''}
    base.update(data)
    return {'id': pid, 'module': 'projects', 'data': base}


def event_chain():
    """事件 → 风险 → 变更 → 任务, plus one log — the full fictional scenario."""
    return [event(), risk(), change(), task(), log(), project(),
            {'id': 'SYS-1', 'module': 'systems', 'data': {'名称': '核心系统'}},
            {'id': 'PPL-1', 'module': 'people', 'data': {'姓名': '张三'}}]


def nodes_by_id(result):
    return {node['id']: node for node in result['nodes']}


def stages_of(result):
    return {node['stage'] for node in result['nodes']}


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
def test_missing_record_id_raises_fixed_error():
    with pytest.raises(ValueError) as info:
        chain(event_chain(), 'NOPE')
    assert str(info.value) in SAFE_LIFECYCLE_ERRORS
    assert str(info.value) == '生命周期查询记录不存在'


def test_archived_record_is_not_queryable():
    records = event_chain()
    records.append({'id': 'INC-9', 'module': 'incidents', 'archived': True,
                    'data': {'名称': '已归档事件'}})
    with pytest.raises(ValueError) as info:
        chain(records, 'INC-9')
    assert str(info.value) in SAFE_LIFECYCLE_ERRORS


@pytest.mark.parametrize('bad', [None, 'INC-1', 5, {'INC-1': 'x'}, ('INC-1',)])
def test_non_list_records_raise_fixed_error(bad):
    with pytest.raises(ValueError) as info:
        chain(bad, 'INC-1')
    assert str(info.value) in SAFE_LIFECYCLE_ERRORS
    assert str(info.value) == '生命周期输入记录无效'


@pytest.mark.parametrize('bad_id', [None, '', '   ', 7, [], {}])
def test_bad_record_id_raises_fixed_error(bad_id):
    with pytest.raises(ValueError) as info:
        chain(event_chain(), bad_id)
    assert str(info.value) in SAFE_LIFECYCLE_ERRORS


@pytest.mark.parametrize('bad_depth', [0, -1, MAX_DEPTH_LIMIT + 1, 99, True, '2', None, 2.0])
def test_bad_max_depth_raises_chain_too_long(bad_depth):
    with pytest.raises(ValueError) as info:
        chain(event_chain(), 'INC-1', max_depth=bad_depth)
    assert str(info.value) in SAFE_LIFECYCLE_ERRORS
    assert str(info.value) == '生命周期链条过长'


def test_unknown_module_record_is_rejected():
    records = [{'id': 'X-1', 'module': 'not_a_module', 'data': {'名称': 'x'}}]
    with pytest.raises(ValueError) as info:
        chain(records, 'X-1')
    assert str(info.value) in SAFE_LIFECYCLE_ERRORS
    assert str(info.value) == '生命周期关联字段不符合模块定义'


def test_record_without_data_is_rejected():
    records = [{'id': 'INC-1', 'module': 'incidents'}]
    with pytest.raises(ValueError) as info:
        chain(records, 'INC-1')
    assert str(info.value) in SAFE_LIFECYCLE_ERRORS


def test_stage_progress_rejects_bad_input():
    with pytest.raises(ValueError) as info:
        stage_progress('not-a-list')
    assert str(info.value) in SAFE_LIFECYCLE_ERRORS


def test_iterating_every_error_message_stays_fixed():
    cases = []
    with pytest.raises(ValueError) as info:
        chain(event_chain(), 'NOPE')
    cases.append(str(info.value))
    with pytest.raises(ValueError) as info:
        chain('bad', 'INC-1')
    cases.append(str(info.value))
    with pytest.raises(ValueError) as info:
        chain(event_chain(), 'INC-1', max_depth=0)
    cases.append(str(info.value))
    with pytest.raises(ValueError) as info:
        chain([{'id': 'X-1', 'module': 'nope', 'data': {}}], 'X-1')
    cases.append(str(info.value))
    with pytest.raises(ValueError) as info:
        stage_progress(3)
    cases.append(str(info.value))
    assert len(cases) == 5
    for message in cases:
        assert message in SAFE_LIFECYCLE_ERRORS


# --------------------------------------------------------------------------- #
# shape
# --------------------------------------------------------------------------- #
def test_stages_and_labels_are_fixed():
    assert STAGES == ('event', 'cause', 'change', 'verify', 'review')
    assert STAGE_LABELS == {'event': '事件', 'cause': '根因', 'change': '变更',
                            'verify': '验证', 'review': '复盘'}
    assert set(STAGE_LABELS) == set(STAGES)


def test_link_fields_use_real_schema_fields():
    from itmanager.schema import MODULES
    for field, targets in LINK_FIELDS.items():
        assert isinstance(targets, tuple) and targets
        for module in targets:
            assert module in MODULES
        owners = [module for module, (_, fields) in MODULES.items() if field in fields]
        assert owners, field
    for field in ('关联ID', '项目ID', '父任务ID', '整改任务ID', '后续任务ID', '系统ID',
                  '人员ID', '来源ID', '规划ID'):
        assert field in LINK_FIELDS


def test_single_node_chain_has_only_root():
    records = [event(根因='', 后续任务ID='')]
    result = chain(records, 'INC-1')
    assert result['root'] == {'id': 'INC-1', 'module': 'incidents', 'title': '系统不可用'}
    assert len(result['nodes']) == 1
    node = result['nodes'][0]
    assert node['stage'] == 'event' and node['depth'] == 0 and node['via'] is None
    assert node['status'] == '进行中' and node['date'] == '2024-03-01'
    assert result['related'] == {'tasks': [], 'logs': [], 'risks': [], 'changes': []}
    assert '缺少变更记录' in result['gaps']
    assert '缺少验证证据或恢复演练记录' in result['gaps']
    assert '缺少复盘工作记录' in result['gaps']


def test_related_buckets_are_filled_from_chain():
    result = chain(event_chain(), 'INC-1')
    assert [row['id'] for row in result['related']['risks']] == ['RSK-1']
    assert [row['id'] for row in result['related']['changes']] == ['CHG-1']
    assert [row['id'] for row in result['related']['tasks']] == ['TSK-1']
    assert [row['id'] for row in result['related']['logs']] == ['LOG-1']
    for bucket in result['related'].values():
        for row in bucket:
            assert set(row) == {'id', 'module', 'title'}


# --------------------------------------------------------------------------- #
# expansion
# --------------------------------------------------------------------------- #
def test_multi_level_expansion_event_risk_change_task():
    result = chain(event_chain(), 'INC-1')
    nodes = nodes_by_id(result)
    assert set(nodes) >= {'INC-1', 'RSK-1', 'CHG-1', 'TSK-1', 'LOG-1'}
    assert nodes['RSK-1']['depth'] == 1
    assert nodes['CHG-1']['depth'] == 2          # 变更 sits behind the linked 风险
    assert nodes['TSK-1']['depth'] == 2          # reached via 风险整改任务ID
    assert nodes['RSK-1']['stage'] == 'cause'
    assert nodes['CHG-1']['stage'] == 'change'
    assert nodes['TSK-1']['stage'] == 'verify'
    assert nodes['LOG-1']['stage'] == 'review'
    assert stages_of(result) == set(STAGES)
    # Every lifecycle stage is represented, so no stage-level gap is reported.
    for gap in ('未找到运维事件记录', '事件记录未关联根因/风险分析', '缺少变更记录',
                '缺少验证证据或恢复演练记录', '缺少复盘工作记录'):
        assert gap not in result['gaps']
    # The fixture has no 恢复演练 record, and that gap is reported honestly.
    assert '缺少备份恢复演练记录' in result['gaps']


def test_via_records_the_field_and_direction():
    result = chain(event_chain(), 'INC-1')
    nodes = nodes_by_id(result)
    # RSK-1 carries 关联ID pointing at the event, so it is discovered backwards.
    assert nodes['RSK-1']['via'] == {'from': 'INC-1', 'field': '关联ID',
                                     'direction': 'in'}
    # TSK-1 is reached forwards, through the risk's own 整改任务ID.
    assert nodes['TSK-1']['via'] == {'from': 'RSK-1', 'field': '整改任务ID',
                                     'direction': 'out'}
    # CHG-1 points at the risk with 关联ID: a forward link from RSK-1's side.
    assert nodes['CHG-1']['via'] == {'from': 'RSK-1', 'field': '关联ID',
                                     'direction': 'in'}


def test_bidirectional_links_are_both_discovered():
    # 关联ID lives on the risk and points at the incident: from the risk we must
    # still reach the incident (incoming direction) and the change (outgoing).
    result = chain(event_chain(), 'RSK-1')
    ids = {node['id'] for node in result['nodes']}
    assert {'INC-1', 'CHG-1', 'TSK-1'} <= ids
    reverse = {node['id']: node for node in result['nodes']}
    assert reverse['INC-1']['via'] == {'from': 'RSK-1', 'field': '关联ID',
                                       'direction': 'out'}
    assert reverse['CHG-1']['via'] == {'from': 'RSK-1', 'field': '关联ID',
                                       'direction': 'in'}


def test_duplicate_references_are_deduplicated():
    records = event_chain()
    records.append(change('CHG-2', 关联ID='INC-1'))
    records.append(task('TSK-2', 整改任务ID='INC-1'))
    result = chain(records, 'INC-1')
    ids = [node['id'] for node in result['nodes']]
    assert len(ids) == len(set(ids))
    assert ids.count('CHG-1') == 1 and ids.count('INC-1') == 1


def test_depth_truncation_reports_gap_without_raising():
    result = chain(event_chain(), 'INC-1', max_depth=1)
    nodes = nodes_by_id(result)
    assert max(node['depth'] for node in result['nodes']) == 1
    assert 'TSK-1' not in nodes
    assert any('截断' in gap for gap in result['gaps'])
    assert '缺少验证证据或恢复演练记录' in result['gaps'] or '缺少' in ' '.join(result['gaps'])


def test_default_depth_reaches_four_levels():
    records = event_chain()
    records.append(risk('RSK-2', 关联ID='CHG-1'))
    records.append(change('CHG-3', 关联ID='RSK-2'))
    records.append(task('TSK-3', 整改任务ID='CHG-3'))
    result = chain(records, 'INC-1')
    nodes = nodes_by_id(result)
    assert nodes['TSK-3']['depth'] >= 3
    assert max(node['depth'] for node in result['nodes']) <= MAX_DEPTH_LIMIT


# --------------------------------------------------------------------------- #
# gaps
# --------------------------------------------------------------------------- #
def test_gap_when_change_missing():
    records = [event(后续任务ID='TSK-1'), task(), project(), log(), recovery()]
    result = chain(records, 'INC-1')
    assert '缺少变更记录' in result['gaps']
    assert stages_of(result) >= {'event', 'verify', 'review'}


def test_gap_when_verification_missing():
    records = [event(), risk(), change(), log()]
    result = chain(records, 'INC-1')
    assert '缺少验证证据或恢复演练记录' in result['gaps']
    assert 'verify' not in stages_of(result)
    # The stage-level text already covers it; no redundant second message.
    assert '缺少与事件或风险关联的后续任务/整改任务' not in result['gaps']
    assert '缺少备份恢复演练记录' not in result['gaps']


def test_gap_details_only_when_verify_stage_exists():
    records = [event(), risk(), change(), log(), {'id': 'MTR-1', 'module': 'metrics',
               'data': {'名称': '可用性指标', '关联ID': 'INC-1', '证据': '报表'}}]
    result = chain(records, 'INC-1')
    assert stages_of(result) >= {'event', 'cause', 'change', 'verify', 'review'}
    assert '缺少与事件或风险关联的后续任务/整改任务' in result['gaps']
    assert '缺少备份恢复演练记录' in result['gaps']
    assert '缺少验证证据或恢复演练记录' not in result['gaps']


def test_gap_when_review_missing():
    records = [event(), risk(), change(), task(), project(), recovery()]
    result = chain(records, 'INC-1')
    assert '缺少复盘工作记录' in result['gaps']
    assert '工作记录中未见与事件或风险关联的记录' not in result['gaps']
    assert '缺少变更记录' not in result['gaps']
    assert '缺少验证证据或恢复演练记录' not in result['gaps']


def test_gap_review_detail_only_when_review_stage_exists():
    # The chain already has a log, but none of the logs tracks a record the
    # 事件/风险 point at, so the finer-grained review gap must still be reported.
    records = event_chain() + [log('LOG-2', 关联ID='LOG-1')]
    result = chain(records, 'INC-1')
    assert {'LOG-1', 'LOG-2'} <= set(nodes_by_id(result))
    assert '缺少复盘工作记录' not in result['gaps']
    assert '工作记录中未见与事件或风险关联的记录' in result['gaps']


def test_no_review_detail_gap_when_log_tracks_a_linked_record():
    # A log whose 关联ID follows the 事件→任务→验证 chain clears the review gaps.
    records = event_chain() + [log('LOG-2', 关联ID='TSK-1')]
    result = chain(records, 'INC-1')
    assert 'review' in stages_of(result)
    assert '缺少复盘工作记录' not in result['gaps']
    assert '工作记录中未见与事件或风险关联的记录' not in result['gaps']


def test_gap_when_cause_missing_and_root_cause_field_empty():
    records = [event(根因=''), change(关联ID='INC-1'), task(), project(), recovery(),
               log()]
    result = chain(records, 'INC-1')
    assert '事件记录未关联根因/风险分析' in result['gaps']
    assert '事件记录的根因字段为空，根因尚未记录' in result['gaps']


def test_no_gaps_when_full_chain_present():
    records = event_chain()
    records[0] = event(根因='配置漂移', 后续任务ID='TSK-1')
    records.append(recovery())
    records.append(risk('RSK-2', 关联ID='INC-1'))
    records.append(log('LOG-2', 关联ID='RCV-1'))
    result = chain(records, 'INC-1')
    assert 'verify' in stages_of(result) and 'review' in stages_of(result)
    assert result['gaps'] == []


DANGLING_GAP = '存在指向不存在记录或已归档记录的关联ID，链条可能不完整'


def test_dangling_reference_is_reported_as_gap():
    records = [event(后续任务ID='TSK-MISSING'), change(关联ID='INC-1'), log(),
               recovery()]
    result = chain(records, 'INC-1')
    assert 'changes' in {node['module'] for node in result['nodes']}
    assert DANGLING_GAP in result['gaps']
    assert 'TSK-MISSING' not in nodes_by_id(result)


# --------------------------------------------------------------------------- #
# summarize_chain
# --------------------------------------------------------------------------- #
def test_summarize_counts_and_complete_semantics():
    result = chain(event_chain(), 'INC-1')
    summary = summarize_chain(result)
    assert summary['total'] == len(result['nodes'])
    assert set(summary['by_stage']) == set(STAGES)
    assert summary['by_stage']['event'] == 1
    assert summary['by_stage']['change'] == 1
    assert summary['by_stage']['verify'] >= 1
    assert summary['by_stage']['review'] == 1
    assert summary['gaps'] == len(result['gaps'])
    assert summary['complete'] is True


def test_summarize_event_and_change_only_is_not_complete():
    records = [event(), change(关联ID='INC-1')]
    result = chain(records, 'INC-1')
    summary = summarize_chain(result)
    assert summary['by_stage']['event'] == 1
    assert summary['by_stage']['change'] == 1
    assert summary['by_stage']['verify'] == 0
    assert summary['complete'] is False
    assert summary['gaps'] >= 1


def test_summarize_missing_cause_and_review_still_complete_when_three_stages_present():
    records = [event(后续任务ID='TSK-1'), change(关联ID='INC-1'), task(),
               project(), recovery()]
    result = chain(records, 'INC-1')
    summary = summarize_chain(result)
    assert summary['by_stage']['cause'] == 0
    assert summary['by_stage']['review'] == 0
    assert summary['complete'] is True          # coverage only, not "resolved"
    assert '缺少复盘工作记录' in result['gaps']


def test_summarize_tolerates_bad_input():
    assert summarize_chain(None) == {'total': 0, 'by_stage': {stage: 0 for stage in STAGES},
                                     'gaps': 0, 'complete': False}
    assert summarize_chain({'nodes': [None, 'x', {}], 'gaps': 'no'})['total'] == 0


def test_no_stage_is_a_verdict_about_resolution():
    # A chain with every stage but an unfinished status must not claim resolution.
    records = [event(状态='进行中'), risk(), change(状态='进行中'), task(), log()]
    result = chain(records, 'INC-1')
    summary = summarize_chain(result)
    assert summary['complete'] is True
    assert result['nodes'][0]['status'] == '进行中'
    assert not any('已解决' in gap for gap in result['gaps'])


# --------------------------------------------------------------------------- #
# stage_progress
# --------------------------------------------------------------------------- #
def test_stage_progress_counts_every_stage():
    rows = stage_progress(event_chain())
    assert [row['stage'] for row in rows] == list(STAGES)
    assert [row['label'] for row in rows] == [STAGE_LABELS[s] for s in STAGES]
    counts = {row['stage']: row['count'] for row in rows}
    assert counts['event'] == 1                  # INC-1
    assert counts['cause'] == 3                  # RSK-1, SYS-1, PPL-1
    assert counts['change'] == 1                 # CHG-1
    assert counts['verify'] == 2                 # TSK-1, PRJ-1 (事件链无恢复演练)
    assert counts['review'] == 1                 # LOG-1
    assert sum(counts.values()) == 8


def test_stage_progress_is_deterministic_and_skips_bad_rows():
    rows = [None, 'x', 42, {'id': '', 'module': 'tasks', 'data': {}}, event()]
    first = stage_progress(rows)
    second = stage_progress(list(rows))
    assert first == second
    assert sum(row['count'] for row in first) == 1


# --------------------------------------------------------------------------- #
# archived behaviour
# --------------------------------------------------------------------------- #
def test_archived_records_are_excluded_from_chain():
    records = event_chain()
    archived = {'id': 'CHG-ARCH', 'module': 'changes', 'archived': True,
                'data': {'名称': '已归档变更', '关联ID': 'INC-1', '验证证据': 'x'}}
    result = chain(records + [archived], 'INC-1')
    assert 'CHG-ARCH' not in nodes_by_id(result)
    assert result == chain(records, 'INC-1')      # archived record changes nothing


def test_archived_parent_reference_is_skipped_not_dangling():
    records = [{'id': 'TSK-ARCH', 'module': 'tasks', 'archived': True,
                'data': {'名称': '归档父任务'}},
               event(后续任务ID='TSK-2'),
               task('TSK-2', 父任务ID='TSK-ARCH', 整改任务ID=''),
               log(), change(关联ID='INC-1'), recovery()]
    result = chain(records, 'INC-1')
    assert 'TSK-ARCH' not in nodes_by_id(result)
    assert 'TSK-2' in nodes_by_id(result)
    assert DANGLING_GAP in result['gaps']


def test_archived_target_is_treated_as_missing_and_reported():
    records = [event(), {'id': 'RSK-ARCH', 'module': 'risks', 'archived': True,
                         'data': {'名称': '已归档风险', '关联ID': 'INC-1'}},
               change(关联ID='INC-1'), log(), recovery()]
    result = chain(records, 'INC-1')
    assert 'RSK-ARCH' not in nodes_by_id(result)
    assert '缺少变更记录' not in result['gaps']      # CHG-1 is reachable via 关联ID
    assert DANGLING_GAP in result['gaps']           # RSK-ARCH is unreachable


def test_archived_records_are_excluded_from_stage_progress():
    records = event_chain()
    records.append({'id': 'INC-ARCH', 'module': 'incidents', 'archived': True,
                    'data': {'名称': '归档事件'}})
    counts = {row['stage']: row['count'] for row in stage_progress(records)}
    assert counts['event'] == 1


# --------------------------------------------------------------------------- #
# bad data
# --------------------------------------------------------------------------- #
def test_bad_rows_never_break_a_valid_chain():
    records = event_chain() + [None, 'text', 17, [], {},
                               {'id': None, 'module': 'tasks', 'data': {}},
                               {'id': 'BAD-1', 'module': 'incidents'}]
    result = chain(records, 'INC-1')
    ids = {node['id'] for node in result['nodes']}
    assert 'TSK-1' in ids and 'CHG-1' in ids
    assert all(node['id'] for node in result['nodes'])


def test_self_reference_does_not_loop():
    records = [event(关联ID='INC-1'), {'id': 'RSK-SELF', 'module': 'risks',
                                      'data': {'名称': '自引用风险', '关联ID': 'RSK-SELF'}}]
    result = chain(records, 'INC-1')
    ids = [node['id'] for node in result['nodes']]
    assert ids.count('INC-1') == 1
    first = chain(records, 'RSK-SELF')
    assert len(first['nodes']) <= 2


def test_two_record_cycle_is_bounded():
    records = [risk('RSK-A', 关联ID='RSK-B'), risk('RSK-B', 关联ID='RSK-A')]
    result = chain(records, 'RSK-A', max_depth=MAX_DEPTH_LIMIT)
    ids = {node['id'] for node in result['nodes']}
    assert ids == {'RSK-A', 'RSK-B'}
    assert len(result['nodes']) == 2


def test_dangling_reference_does_not_create_a_node():
    records = [event(后续任务ID='TSK-NOPE'), change(), log(), recovery()]
    result = chain(records, 'INC-1')
    assert 'TSK-NOPE' not in nodes_by_id(result)


def test_missing_optional_fields_still_render():
    records = [{'id': 'INC-1', 'module': 'incidents', 'data': {'名称': '极简事件'}}]
    result = chain(records, 'INC-1')
    node = result['nodes'][0]
    assert node['title'] == '极简事件'
    assert node['status'] == '' and node['date'] == ''


# --------------------------------------------------------------------------- #
# title_of
# --------------------------------------------------------------------------- #
def test_title_of_is_safe():
    from itmanager.schema import MODULES
    for module, (_, fields) in MODULES.items():
        assert title_of(module, {fields[0]: ' 标题 '}) == '标题'
        assert title_of(module, {}) == ''
        assert title_of(module, None) == ''
        assert title_of(module, {fields[0]: 123}) == ''
    assert title_of('not_a_module', {'名称': 'x'}) == ''
    assert title_of(None, {'名称': 'x'}) == ''
    assert title_of('tasks', {'名称': '   '}) == ''


def test_module_to_stage_partition_is_total():
    from itmanager.lifecycle import STAGE_MODULES
    from itmanager.schema import MODULES
    assert set(STAGE_MODULES) == set(MODULES)
    assert set(STAGE_MODULES.values()) <= set(STAGES)
    assert STAGE_MODULES['incidents'] == 'event'
    assert STAGE_MODULES['changes'] == 'change'
    assert STAGE_MODULES['logs'] == 'review'


def test_chain_output_is_json_friendly_and_deterministic():
    import json
    records = event_chain()
    first = chain(records, 'INC-1')
    second = chain(list(reversed(records)), 'INC-1')
    assert json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert {node['id'] for node in first['nodes']} == {node['id'] for node in second['nodes']}
    assert first['gaps'] == second['gaps']
