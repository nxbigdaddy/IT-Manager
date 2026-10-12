"""Reminder handling state: pure, offline, deterministic; every fixture is fictional."""
import json
import re
from datetime import date, timedelta

import pytest

from itmanager import reminder_state
from itmanager.reminder_state import (
    ACTIONS, SAFE_STATE_ERRORS, apply_states, state_key, summarize_states, validate_state,
)

TODAY = date(2026, 6, 1)


def state(action='done', rule_key='contract_expiry', record_id='REC-1', **overrides):
    base = {'rule_key': rule_key, 'record_id': record_id, 'action': action,
            'reason': '', 'until': None, 'record_version': None,
            'actor': 'tester', 'updated': '2026-06-01T00:00:00+00:00'}
    base.update(overrides)
    return base


def item(rule_key='contract_expiry', record_id='REC-1', **overrides):
    base = {'rule_key': rule_key, 'rule_name': '合同到期提醒', 'severity': '高',
            'module': 'contracts', 'record_id': record_id, 'title': '示例合同',
            'date': '2026-06-20', 'days_left': 19, 'overdue_days': 0,
            'message': '到期日期 2026-06-20 还有 19 天到期',
            'sort': [0, 19, 'contracts', '示例合同']}
    base.update(overrides)
    return base


def days(offset):
    return (TODAY + timedelta(days=offset)).isoformat()


def test_state_key_is_stable_unique_and_usable_as_an_object_id():
    first = state_key('contract_expiry', 'REC-1')
    assert first == state_key('contract_expiry', 'REC-1')
    assert first != state_key('contract_expiry', 'REC-2')
    assert first != state_key('task_overdue', 'REC-1')
    assert first != state_key('a-b', 'c')
    assert first != state_key('a', 'b-c')
    assert first.replace('-', '').replace('_', '').isalnum()
    assert len(first) <= 120
    long_key = state_key('r' * 90, 'i' * 90)
    assert len(long_key) <= 120
    assert long_key == state_key('r' * 90, 'i' * 90)
    assert long_key != state_key('r' * 89, 'i' * 90)
    assert json.dumps(state(state_key('contract_expiry', 'REC-1')), ensure_ascii=False)


@pytest.mark.parametrize('rule_key,record_id', [
    pytest.param(None, 'REC-1'), pytest.param('contract_expiry', None),
    pytest.param(7, 'REC-1'), pytest.param('contract_expiry', 7),
    pytest.param('', 'REC-1'), pytest.param('contract_expiry', ''),
    pytest.param('   ', 'REC-1'), pytest.param(' contract_expiry', 'REC-1'),
    pytest.param('contract_expiry', 'REC-1 '),
])
def test_state_key_rejects_invalid_input(rule_key, record_id):
    with pytest.raises(ValueError) as excinfo:
        state_key(rule_key, record_id)
    assert str(excinfo.value) == '提醒处理状态键无效'
    assert str(excinfo.value) in SAFE_STATE_ERRORS


def test_state_key_rejects_parts_outside_the_object_id_charset():
    # Rule keys are ASCII identifiers in practice; anything else cannot become a
    # shareable object id and is refused rather than silently mangled.
    for rule_key, record_id in [('合同到期', 'REC-1'), ('contract expiry', 'REC-1'),
                                ('contract_expiry', 'REC/1'), ('a@b', 'c'),
                                ('a', 'b@c'), ('contract_expiry ', 'REC-1')]:
        with pytest.raises(ValueError) as excinfo:
            state_key(rule_key, record_id)
        assert str(excinfo.value) == '提醒处理状态键无效'


def test_safe_errors_are_the_only_messages_and_actions_are_fixed():
    assert SAFE_STATE_ERRORS == frozenset({
        '提醒处理动作无效', '提醒处理状态包含未知字段', '稍后提醒必须给出未来日期',
        '忽略提醒必须填写原因', '提醒处理原因过长', '提醒处理状态记录版本无效',
        '提醒处理状态键无效'})
    assert ACTIONS == ('done', 'snooze', 'dismiss')
    assert all(isinstance(message, str) and message for message in SAFE_STATE_ERRORS)


@pytest.mark.parametrize('payload', [[], 'done', None, 7, True])
def test_validate_state_rejects_non_dict_payloads(payload):
    with pytest.raises(ValueError) as excinfo:
        validate_state(payload, TODAY)
    assert str(excinfo.value) == '提醒处理状态包含未知字段'
    assert str(excinfo.value) in SAFE_STATE_ERRORS


@pytest.mark.parametrize('patch,message', [
    ({'extra': 1}, '提醒处理状态包含未知字段'),
    ({'action': 'boom'}, '提醒处理动作无效'),
    ({'action': None}, '提醒处理动作无效'),
    ({'action': '完成的'}, '提醒处理动作无效'),
    ({'rule_key': '合同到期'}, '提醒处理状态键无效'),
    ({'rule_key': ''}, '提醒处理状态键无效'),
    ({'rule_key': 7}, '提醒处理状态键无效'),
    ({'rule_key': ' contract_expiry'}, '提醒处理状态键无效'),
    ({'record_id': None}, '提醒处理状态键无效'),
    ({'record_id': '  '}, '提醒处理状态键无效'),
    ({'record_id': 'REC/1'}, '提醒处理状态键无效'),
    ({'action': 'snooze', 'until': None}, '稍后提醒必须给出未来日期'),
    ({'action': 'snooze', 'until': '2026/07/01'}, '稍后提醒必须给出未来日期'),
    ({'action': 'snooze', 'until': '2026-06-01'}, '稍后提醒必须给出未来日期'),
    ({'action': 'snooze', 'until': '2026-05-31'}, '稍后提醒必须给出未来日期'),
    ({'action': 'dismiss'}, '忽略提醒必须填写原因'),
    ({'action': 'dismiss', 'reason': '   '}, '忽略提醒必须填写原因'),
    ({'action': 'dismiss', 'reason': 7}, '提醒处理原因过长'),
    ({'reason': 'x' * 501}, '提醒处理原因过长'),
    ({'record_version': 0}, '提醒处理状态记录版本无效'),
    ({'record_version': -1}, '提醒处理状态记录版本无效'),
    ({'record_version': '3'}, '提醒处理状态记录版本无效'),
    ({'record_version': 1.0}, '提醒处理状态记录版本无效'),
    ({'actor': 7}, '提醒处理状态包含未知字段'),
    ({'updated': 123}, '提醒处理状态包含未知字段'),
])
def test_validate_state_rejects_each_contract_violation(patch, message):
    data = state()
    data.update(patch)
    with pytest.raises(ValueError) as excinfo:
        validate_state(data, TODAY)
    assert str(excinfo.value) == message
    assert str(excinfo.value) in SAFE_STATE_ERRORS


def test_validate_state_accepts_every_valid_action():
    assert validate_state(state('done'), TODAY) is None
    assert validate_state(state('snooze', until=days(1)), TODAY) is None
    assert validate_state(state('dismiss', reason='误报，已人工确认'), TODAY) is None
    assert validate_state(state('done', record_version=3, actor='', updated=''), TODAY) is None
    assert validate_state(state('done', until='not-a-date'), TODAY) is None
    assert validate_state({k: v for k, v in state().items() if k != 'until'}, TODAY) is None
    assert validate_state(state('snooze', until='2099-01-01'), None) is None


def test_validate_state_rejects_unknown_fields_even_when_otherwise_valid():
    data = state('dismiss', reason='误报', 处理人='张三')
    with pytest.raises(ValueError) as excinfo:
        validate_state(data, TODAY)
    assert str(excinfo.value) == '提醒处理状态包含未知字段'


def test_snooze_in_the_future_hides_the_item():
    items = [item(), item(record_id='REC-2')]
    result = apply_states(items, [state('snooze', until=days(30))], TODAY)
    assert len(result) == 1
    assert result[0]['record_id'] == 'REC-2'
    assert isinstance(result[0], dict)
    for key in items[1]:
        assert result[0][key] == items[1][key]
    assert 'handled' not in result[0] and 'resumed' not in result[0]


@pytest.mark.parametrize('until', [TODAY.isoformat(), days(-1), days(-90)])
def test_snooze_expiring_today_or_earlier_reappears_as_resumed(until):
    original = item()
    result = apply_states([original], [state('snooze', until=until)], TODAY)
    assert len(result) == 1
    assert result[0]['resumed'] is True
    assert {k: v for k, v in result[0].items() if k != 'resumed'} == original
    assert original == item()


def test_broken_snooze_input_reappears_as_resumed_never_hidden():
    # Validation refuses these; if one still reaches apply_states it must not hide
    # a live reminder, and the marker still tells the user it was snoozed before.
    for patch in ({'until': None}, {'until': '2026/06/30'}, {'until': ''}, {'until': 20260630}):
        data = state('snooze')
        data.update(patch)
        result = apply_states([item()], [data], TODAY)
        assert result == [dict(item(), resumed=True)]


@pytest.mark.parametrize('action', ['done', 'dismiss'])
def test_handled_actions_hide_by_default_and_return_marked_when_requested(action):
    data = state(action, reason='误报' if action == 'dismiss' else '')
    visible = apply_states([item()], [data], TODAY)
    assert visible == []
    kept = apply_states([item()], [data], TODAY, include_handled=True)
    assert len(kept) == 1
    assert kept[0]['handled'] == action
    marker = {k: v for k, v in kept[0].items() if k != 'handled'}
    assert marker == item()


def test_record_version_change_expires_the_state_and_the_reminder_returns():
    assert apply_states([item(record_version=1)], [state(record_version=1)], TODAY) == []
    assert apply_states([item(record_version=2)], [state(record_version=1)], TODAY) == [
        item(record_version=2)]
    assert apply_states([item()], [state(record_version=9)], TODAY) == []
    stale = apply_states([item(record_version=2)],
                         [state('snooze', until=days(7), record_version=1)], TODAY)
    assert stale == [item(record_version=2)]
    assert 'resumed' not in stale[0]


def test_latest_updated_wins_and_missing_updated_keeps_the_last_input():
    earlier = state('done', updated='2026-06-01T00:00:00+00:00')
    later = state('snooze', until=days(5), updated='2026-06-02T00:00:00+00:00')
    assert apply_states([item()], [earlier, later], TODAY) == []
    assert apply_states([item()], [later, earlier], TODAY) == []
    snoozed = {k: v for k, v in state('snooze', until=days(5)).items() if k != 'updated'}
    finished = {k: v for k, v in state('done').items() if k != 'updated'}
    assert snoozed['action'] == 'snooze' and finished['action'] == 'done'
    # Input order decides when neither state carries an updated stamp: the later wins.
    assert apply_states([item()], [snoozed, finished], TODAY, include_handled=True) == [
        dict(item(), handled='done')]
    assert apply_states([item()], [finished, snoozed], TODAY) == []


def test_latest_state_among_many_for_one_key_decides_visibility():
    states = [state('done', updated='2026-05-01T00:00:00+00:00'),
              state('dismiss', reason='误报', updated='2026-05-20T00:00:00+00:00'),
              state('done', updated='2026-05-10T00:00:00+00:00')]
    assert apply_states([item()], states, TODAY) == []
    kept = apply_states([item()], states, TODAY, include_handled=True)
    assert len(kept) == 1 and kept[0]['handled'] == 'dismiss'
    newest = state('snooze', until=days(3), updated='2026-06-03T00:00:00+00:00')
    assert apply_states([item()], states + [newest], TODAY, include_handled=True) == []


def test_order_keys_and_types_are_untouched_for_visible_items():
    items = [item(rule_key='task_overdue', record_id='REC-3'),
             item(rule_key='contract_expiry', record_id='REC-1'),
             item(rule_key='risk_review_expiry', record_id='REC-9')]
    result = apply_states(items, [state('done', rule_key='task_overdue', record_id='REC-3')], TODAY)
    assert [row['record_id'] for row in result] == ['REC-1', 'REC-9']
    assert [row['sort'] for row in result] == [items[1]['sort'], items[2]['sort']]
    assert result[0] == items[1] and result[1] == items[2]
    assert set(result[0]) == set(items[1])


def test_states_for_other_keys_never_affect_unrelated_items():
    unrelated = state('done', rule_key='stale_open_record', record_id='OTHER')
    assert apply_states([item()], [unrelated], TODAY) == [item()]
    merged = apply_states([item()], [unrelated, state('done')], TODAY)
    assert merged == []


def test_invalid_state_entries_are_skipped_safely_without_raising():
    junk = [None, 'done', 7, [], {'rule_key': 'contract_expiry'},
            {'rule_key': '合同到期', 'record_id': 'REC-1', 'action': 'done'},
            {'rule_key': 'contract_expiry', 'record_id': 'REC-1'},
            {'rule_key': 'contract_expiry', 'record_id': 'REC-1', 'action': 'boom'}]
    assert apply_states([item()], junk, TODAY) == [item()]
    assert apply_states([item()], None, TODAY) == [item()]
    assert apply_states([item()], {'rule_key': 'contract_expiry'}, TODAY) == [item()]
    assert apply_states([], junk, TODAY) == []
    assert apply_states([None, 'x', 7], [], TODAY) == []
    assert apply_states(None, junk, TODAY) == []
    assert apply_states('items', junk, TODAY) == []
    with pytest.raises(ValueError) as excinfo:
        apply_states([item()], junk, '2026-06-01')
    assert str(excinfo.value) in SAFE_STATE_ERRORS


def test_summarize_states_counts_each_marker_and_defaults_to_active():
    mixed = [item(record_id='R1'), item(record_id='R2', resumed=True),
             item(record_id='R3', handled='done'), item(record_id='R4', handled='dismiss'),
             item(record_id='R5')]
    summary = summarize_states(mixed)
    assert summary == {'total': 5, 'active': 2, 'handled': 2, 'snoozed': 1, 'dismissed': 1}
    assert set(summary) == {'total', 'active', 'handled', 'snoozed', 'dismissed'}
    assert summary['total'] == summary['active'] + summary['handled'] + summary['snoozed']
    assert summary['handled'] == summary['dismissed'] + 1
    assert summarize_states([]) == {'total': 0, 'active': 0, 'handled': 0,
                                    'snoozed': 0, 'dismissed': 0}
    assert summarize_states(None)['total'] == 0
    assert summarize_states([None, 'x', 7, {'handled': 'weird'}, {}]) == {
        'total': 2, 'active': 2, 'handled': 0, 'snoozed': 0, 'dismissed': 0}


def test_apply_then_summarize_round_trip_is_json_serializable_and_pure():
    items = [item(record_id='R1'), item(record_id='R2'), item(record_id='R3')]
    states = [state(record_id='R1'), state('snooze', record_id='R2', until=days(4)),
              state('dismiss', record_id='R3', reason='误报')]
    handled = apply_states(items, states, TODAY, include_handled=True)
    assert [row['record_id'] for row in handled] == ['R1', 'R3']
    assert summarize_states(handled) == {'total': 2, 'active': 0, 'handled': 2,
                                         'snoozed': 0, 'dismissed': 1}
    assert summarize_states(apply_states(items, states, TODAY)) == {
        'total': 0, 'active': 0, 'handled': 0, 'snoozed': 0, 'dismissed': 0}
    assert items == [item(record_id='R1'), item(record_id='R2'), item(record_id='R3')]
    assert json.dumps(handled, ensure_ascii=False)
