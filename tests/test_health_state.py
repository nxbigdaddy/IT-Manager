"""Data-health handling state: pure, offline, deterministic; every fixture is fictional."""
import json
import re

import pytest

from itmanager import health_state
from itmanager.health_state import (
    SAFE_HEALTH_STATE_ERRORS, STATUSES, apply_states, finding_key, summarize_states,
    validate_state,
)


def finding(code='missing_owner', record_id='REC-1', field='负责人', **overrides):
    base = {'code': code, 'severity': '中', 'module': 'contracts', 'record_id': record_id,
            'title': '示例合同', 'field': field, 'detail': '负责人类字段为空，责任无法落实',
            'suggestion': '请指定负责人并确认其已知悉该职责。'}
    base.update(overrides)
    return base


def state(status='resolved', code='missing_owner', record_id='REC-1', field='负责人', **overrides):
    base = {'code': code, 'record_id': record_id, 'field': field, 'status': status,
            'reason': '', 'task_id': None, 'record_version': None, 'actor': 'tester',
            'updated': '2026-06-01T00:00:00+00:00'}
    base.update(overrides)
    return base


def test_finding_key_is_stable_unique_and_usable_as_an_object_id():
    first = finding_key('missing_owner', 'REC-1', '负责人')
    assert first == finding_key('missing_owner', 'REC-1', '负责人')
    assert first != finding_key('missing_owner', 'REC-1', '负责人ID')
    assert first != finding_key('missing_owner', 'REC-2', '负责人')
    assert first != finding_key('stale_open', 'REC-1', '负责人')
    assert first != finding_key('a-b', 'c', 'd')
    assert first != finding_key('a', 'b-c', 'd')
    assert first != finding_key('a', 'b', 'c-d')
    assert first.replace('-', '').replace('_', '').isalnum()
    assert len(first) <= 120
    long_key = finding_key('c' * 90, 'i' * 90, 'f' * 90)
    assert len(long_key) <= 120
    assert long_key == finding_key('c' * 90, 'i' * 90, 'f' * 90)
    assert long_key != finding_key('c' * 89, 'i' * 90, 'f' * 90)


@pytest.mark.parametrize('code,record_id,field', [
    pytest.param(None, 'REC-1', '负责人'), pytest.param('missing_owner', None, '负责人'),
    pytest.param('missing_owner', 'REC-1', None), pytest.param(7, 'REC-1', '负责人'),
    pytest.param('missing_owner', 7, '负责人'), pytest.param('missing_owner', 'REC-1', 7),
    pytest.param('', 'REC-1', '负责人'), pytest.param('missing_owner', '', '负责人'),
    pytest.param('missing_owner', 'REC-1', ''), pytest.param('missing_owner', 'REC-1', '   '),
    pytest.param('missing_owner ', 'REC-1', '负责人'), pytest.param('missing_owner', 'REC-1 ', '负责人'),
])
def test_finding_key_rejects_invalid_input(code, record_id, field):
    with pytest.raises(ValueError) as excinfo:
        finding_key(code, record_id, field)
    assert str(excinfo.value) == '整改状态键无效'
    assert str(excinfo.value) in SAFE_HEALTH_STATE_ERRORS


def test_finding_key_encodes_chinese_field_names_into_ascii_ids():
    # health.scan field values are Chinese schema names (负责人, 截止日期 ...), so the
    # encoder is the normal path for real findings: keys stay pure ASCII and unique.
    chinese = finding_key('missing_owner', 'REC-1', '负责人')
    assert chinese == finding_key('missing_owner', 'REC-1', '负责人')
    assert re.fullmatch(r'[A-Za-z0-9_-]+', chinese)
    assert len(chinese) <= 120
    assert chinese != finding_key('missing_owner', 'REC-1', '责任人')
    assert chinese != finding_key('missing_owner', 'REC-2', '负责人')
    assert chinese != finding_key('stale_open', 'REC-1', '负责人')
    for code, record_id, field in [('缺失负责人', 'REC-1', '负责人'),
                                   ('missing owner', 'REC-1', '负责人'),
                                   ('missing_owner', 'REC/1', '负责人'),
                                   ('missing_owner', 'REC-1', '技术负责人')]:
        key = finding_key(code, record_id, field)
        assert key == finding_key(code, record_id, field)
        assert re.fullmatch(r'[A-Za-z0-9_-]+', key)
        assert len(key) <= 120
    long_key = finding_key('c' * 90, 'i' * 90, 'f' * 90)
    assert len(long_key) <= 120 and long_key == finding_key('c' * 90, 'i' * 90, 'f' * 90)
    assert long_key != finding_key('c' * 89, 'i' * 90, 'f' * 90)


def test_safe_errors_are_the_only_messages_and_statuses_are_fixed():
    assert SAFE_HEALTH_STATE_ERRORS == frozenset({
        '整改状态无效', '整改状态包含未知字段', '误报忽略必须填写原因', '整改说明过长',
        '整改状态记录版本无效', '整改状态键无效', '整改任务ID无效'})
    assert STATUSES == ('resolved', 'task_created', 'ignored')
    assert all(isinstance(message, str) and message for message in SAFE_HEALTH_STATE_ERRORS)


@pytest.mark.parametrize('payload', [[], 'resolved', None, 7, True])
def test_validate_state_rejects_non_dict_payloads(payload):
    with pytest.raises(ValueError) as excinfo:
        validate_state(payload)
    assert str(excinfo.value) == '整改状态包含未知字段'
    assert str(excinfo.value) in SAFE_HEALTH_STATE_ERRORS


@pytest.mark.parametrize('patch,message', [
    ({'extra': 1}, '整改状态包含未知字段'),
    ({'status': 'fixed'}, '整改状态无效'),
    ({'status': None}, '整改状态无效'),
    ({'status': '已整改'}, '整改状态无效'),
    ({'code': ''}, '整改状态键无效'),
    ({'code': None}, '整改状态键无效'),
    ({'code': ' missing_owner'}, '整改状态键无效'),
    ({'record_id': 7}, '整改状态键无效'),
    ({'record_id': '  '}, '整改状态键无效'),
    ({'field': None}, '整改状态键无效'),
    ({'field': ''}, '整改状态键无效'),
    ({'field': ' 负责人'}, '整改状态键无效'),
    ({'status': 'ignored'}, '误报忽略必须填写原因'),
    ({'status': 'ignored', 'reason': '   '}, '误报忽略必须填写原因'),
    ({'status': 'ignored', 'reason': 7}, '整改说明过长'),
    ({'reason': 'x' * 501}, '整改说明过长'),
    ({'task_id': ''}, '整改任务ID无效'),
    ({'task_id': 7}, '整改任务ID无效'),
    ({'task_id': 'TASK 1'}, '整改任务ID无效'),
    ({'task_id': 'task/1'}, '整改任务ID无效'),
    ({'record_version': 0}, '整改状态记录版本无效'),
    ({'record_version': -2}, '整改状态记录版本无效'),
    ({'record_version': '2'}, '整改状态记录版本无效'),
    ({'record_version': 2.0}, '整改状态记录版本无效'),
    ({'actor': 7}, '整改状态包含未知字段'),
    ({'updated': 123}, '整改状态包含未知字段'),
])
def test_validate_state_rejects_each_contract_violation(patch, message):
    data = state()
    data.update(patch)
    with pytest.raises(ValueError) as excinfo:
        validate_state(data)
    assert str(excinfo.value) == message
    assert str(excinfo.value) in SAFE_HEALTH_STATE_ERRORS


def test_validate_state_accepts_every_valid_status():
    assert validate_state(state('resolved')) is None
    assert validate_state(state('task_created', task_id='TASKS-abc123')) is None
    assert validate_state(state('ignored', reason='误报，字段本就不适用')) is None
    assert validate_state(state('resolved', task_id=None, actor='', updated='')) is None
    without_task = {k: v for k, v in state('task_created').items() if k != 'task_id'}
    assert validate_state(without_task) is None


def test_validate_state_rejects_unknown_fields_even_when_otherwise_valid():
    data = state('ignored', reason='误报', 处理人='张三')
    with pytest.raises(ValueError) as excinfo:
        validate_state(data)
    assert str(excinfo.value) == '整改状态包含未知字段'


@pytest.mark.parametrize('status', ['resolved', 'task_created', 'ignored'])
def test_handled_statuses_hide_by_default_and_return_marked_when_requested(status):
    data = state(status, reason='误报' if status == 'ignored' else '',
                 task_id='TASKS-1' if status == 'task_created' else None)
    assert apply_states([finding()], [data]) == []
    kept = apply_states([finding()], [data], include_resolved=True)
    assert len(kept) == 1
    assert kept[0]['handled'] == status
    assert {k: v for k, v in kept[0].items() if k != 'handled'} == finding()


def test_record_version_change_expires_the_state_and_the_finding_returns():
    assert apply_states([finding(record_version=1)], [state(record_version=1)]) == []
    assert apply_states([finding(record_version=2)], [state(record_version=1)]) == [
        finding(record_version=2)]
    assert apply_states([finding()], [state(record_version=9)]) == []
    stale = apply_states([finding(record_version=3)],
                         [state('ignored', reason='误报', record_version=3 - 2)])
    assert stale == [finding(record_version=3)]
    assert 'handled' not in stale[0]


def test_latest_updated_wins_and_missing_updated_keeps_the_last_input():
    earlier = state('resolved', updated='2026-06-01T00:00:00+00:00')
    later = state('ignored', reason='误报', updated='2026-06-02T00:00:00+00:00')
    # The newest stamp wins regardless of input order.
    assert apply_states([finding()], [earlier, later]) == []
    assert apply_states([finding()], [later, earlier]) == []
    first = apply_states([finding()], [earlier, later], include_resolved=True)
    assert len(first) == 1 and first[0]['handled'] == 'ignored'
    swapped = apply_states([finding()], [later, earlier], include_resolved=True)
    assert len(swapped) == 1 and swapped[0]['handled'] == 'ignored'
    # Without an updated stamp the later input wins, stably.
    resolved = {k: v for k, v in state('resolved').items() if k != 'updated'}
    ignored = {k: v for k, v in state('ignored', reason='误报').items() if k != 'updated'}
    assert apply_states([finding()], [resolved, ignored], include_resolved=True) == [
        dict(finding(), handled='ignored')]
    assert apply_states([finding()], [ignored, resolved], include_resolved=True) == [
        dict(finding(), handled='resolved')]
    assert apply_states([finding()], [ignored, resolved], include_resolved=True) == [
        dict(finding(), handled='resolved')]


def test_latest_state_among_many_for_one_key_decides_visibility():
    states = [state('resolved', updated='2026-05-01T00:00:00+00:00'),
              state('task_created', task_id='TASKS-9', updated='2026-05-25T00:00:00+00:00'),
              state('resolved', updated='2026-05-10T00:00:00+00:00')]
    assert apply_states([finding()], states) == []
    kept = apply_states([finding()], states, include_resolved=True)
    assert len(kept) == 1 and kept[0]['handled'] == 'task_created'


def test_order_and_every_other_finding_field_are_untouched():
    findings = [finding(code='missing_date', record_id='REC-3', field='截止日期'),
                finding(code='missing_owner', record_id='REC-1', field='负责人'),
                finding(code='stale_open', record_id='REC-9', field='updated')]
    result = apply_states(findings, [state(record_id='REC-3', field='截止日期',
                                           code='missing_date')])
    assert [row['record_id'] for row in result] == ['REC-1', 'REC-9']
    assert result[0] == findings[1] and result[1] == findings[2]
    assert set(result[0]) == set(findings[1])
    assert result[0]['suggestion'] == findings[1]['suggestion']


def test_field_is_part_of_the_key_so_sibling_findings_stay_visible():
    findings = [finding(field='负责人'), finding(field='技术负责人')]
    result = apply_states(findings, [state(field='负责人')])
    assert len(result) == 1
    assert result[0] == findings[1]
    assert apply_states(findings, [state(field='负责人'), state(field='技术负责人')]) == []


def test_states_for_other_keys_never_affect_unrelated_findings():
    unrelated = state('ignored', reason='误报', record_id='OTHER')
    assert apply_states([finding()], [unrelated]) == [finding()]
    merged = apply_states([finding()], [unrelated, state('resolved')])
    assert merged == []


def test_invalid_state_entries_are_skipped_safely_without_raising():
    junk = [None, 'resolved', 7, [], {'code': 'missing_owner'},
            {'code': '缺失负责人', 'record_id': 'REC-1', 'field': '负责人', 'status': 'resolved'},
            {'code': 'missing_owner', 'record_id': 'REC-1', 'field': '负责人'},
            {'code': 'missing_owner', 'record_id': 'REC-1', 'field': '负责人', 'status': 'boom'}]
    assert apply_states([finding()], junk) == [finding()]
    assert apply_states([finding()], None) == [finding()]
    assert apply_states([finding()], {'code': 'missing_owner'}) == [finding()]
    assert apply_states([], junk) == []
    assert apply_states([None, 'x', 7], [], include_resolved=True) == []
    assert apply_states(None, junk) == []
    assert apply_states('findings', junk) == []


def test_summarize_states_counts_each_marker_and_defaults_to_active():
    mixed = [finding(record_id='R1'), finding(record_id='R2', handled='resolved'),
             finding(record_id='R3', handled='task_created'),
             finding(record_id='R4', handled='ignored'), finding(record_id='R5')]
    summary = summarize_states(mixed)
    assert summary == {'total': 5, 'active': 2, 'task_created': 1, 'resolved': 1, 'ignored': 1}
    assert set(summary) == {'total', 'active', 'task_created', 'resolved', 'ignored'}
    assert summary['total'] == summary['active'] + summary['task_created'] + summary['resolved'] + summary['ignored']
    assert summarize_states([]) == {'total': 0, 'active': 0, 'task_created': 0,
                                    'resolved': 0, 'ignored': 0}
    assert summarize_states(None)['total'] == 0
    assert summarize_states([None, 'x', 7, {'handled': 'weird'}, {}]) == {
        'total': 2, 'active': 2, 'task_created': 0, 'resolved': 0, 'ignored': 0}


def test_apply_then_summarize_round_trip_is_json_serializable_and_pure():
    findings = [finding(record_id='R1'), finding(record_id='R2'), finding(record_id='R3')]
    states = [state(record_id='R1'), state('task_created', record_id='R2', task_id='TASKS-2'),
              state('ignored', record_id='R3', reason='误报')]
    handled = apply_states(findings, states, include_resolved=True)
    assert [row['record_id'] for row in handled] == ['R1', 'R2', 'R3']
    assert summarize_states(handled) == {'total': 3, 'active': 0, 'task_created': 1,
                                         'resolved': 1, 'ignored': 1}
    assert summarize_states(apply_states(findings, states)) == {
        'total': 0, 'active': 0, 'task_created': 0, 'resolved': 0, 'ignored': 0}
    assert findings == [finding(record_id='R1'), finding(record_id='R2'), finding(record_id='R3')]
    assert json.dumps(handled, ensure_ascii=False)
    assert health_state.STATUSES[0] == 'resolved'
