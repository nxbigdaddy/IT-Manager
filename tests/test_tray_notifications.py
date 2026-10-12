"""Pure tray policy coverage plus offscreen, mocked native-tray integration."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from itmanager.tray_notifications import (
    SAFE_TRAY_ERRORS, TrayController, TrayPolicy, in_quiet_hours, item_key,
    notification_text, select_notifications, severity_rank,
)

UTC = timezone.utc
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


def item(n='1', severity='高', **changes):
    value = {'rule_key': 'contract_expiry', 'record_id': n, 'rule_name': '合同到期',
             'severity': severity, 'title': '示例合同', 'date': '2026-03-20',
             'message': '请安排续签或退出。'}
    value.update(changes)
    return value


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / 'tray.ini'), QSettings.IniFormat)


# ---- pure logic (more than 25 independently collected cases) ----
@pytest.mark.parametrize('field,value,message', [
    ('enabled', 1, '托盘提醒启用状态无效'),
    ('min_severity', '紧急', '托盘提醒等级无效'),
    ('cooldown_minutes', 4, '托盘提醒冷却时间无效'),
    ('cooldown_minutes', 1441, '托盘提醒冷却时间无效'),
    ('cooldown_minutes', True, '托盘提醒冷却时间无效'),
    ('max_per_cycle', 0, '托盘提醒数量无效'),
    ('max_per_cycle', 11, '托盘提醒数量无效'),
    ('max_per_cycle', False, '托盘提醒数量无效'),
    ('quiet_start', '24:00', '托盘安静时段无效'),
    ('quiet_end', '8:00', '托盘安静时段无效'),
])
def test_policy_rejects_bad_values(field, value, message):
    with pytest.raises(ValueError, match=message):
        TrayPolicy(**{field: value})
    assert message in SAFE_TRAY_ERRORS


def test_policy_defaults_and_boundaries():
    assert TrayPolicy() == TrayPolicy(False, '高', 120, 3, '22:00', '08:00')
    assert TrayPolicy(cooldown_minutes=5, max_per_cycle=1)
    assert TrayPolicy(cooldown_minutes=1440, max_per_cycle=10)


@pytest.mark.parametrize('value,expected', [('低', 0), ('中', 1), ('高', 2), ('x', -1), (None, -1)])
def test_severity_rank(value, expected):
    assert severity_rank(value) == expected


@pytest.mark.parametrize('hour,expected', [(21, False), (22, True), (23, True), (0, True), (7, True), (8, False)])
def test_quiet_crosses_midnight(hour, expected):
    assert in_quiet_hours(TrayPolicy(), NOW.replace(hour=hour)) is expected


def test_quiet_same_boundary_is_all_day_and_non_crossing_works():
    assert in_quiet_hours(TrayPolicy(quiet_start='12:00', quiet_end='12:00'), NOW)
    policy = TrayPolicy(quiet_start='09:00', quiet_end='17:00')
    assert in_quiet_hours(policy, NOW)
    assert not in_quiet_hours(policy, NOW.replace(hour=18))


def test_quiet_requires_aware_datetime():
    with pytest.raises(ValueError, match='托盘扫描时间无效'):
        in_quiet_hours(TrayPolicy(), NOW.replace(tzinfo=None))


def test_item_key_stable_distinct_and_safe():
    assert item_key(item()) == item_key(dict(item()))
    assert item_key(item('2')) != item_key(item('1'))
    for bad in (None, [], {}, {'rule_key': 'x'}, {'rule_key': '', 'record_id': '1'},
                {'rule_key': 'x', 'record_id': 1}):
        assert item_key(bad) is None


def test_select_filters_severity_preserves_order_and_does_not_mutate():
    items = [item('1', '中'), item('2', '高'), item('3', '高')]
    original = [dict(x) for x in items]
    result = select_notifications(items, TrayPolicy(max_per_cycle=2), {}, NOW)
    assert [x['record_id'] for x in result['selected']] == ['2', '3']
    assert result['suppressed']['severity'] == 1
    assert items == original


def test_select_quiet_suppresses_without_updating_state():
    result = select_notifications([item()], TrayPolicy(), {}, NOW.replace(hour=23))
    assert not result['selected'] and result['suppressed']['quiet'] == 1
    assert result['new_state'] == {}


def test_select_cooldown_and_expiry():
    first = select_notifications([item()], TrayPolicy(), {}, NOW)
    second = select_notifications([item()], TrayPolicy(), first['new_state'], NOW + timedelta(minutes=119))
    assert second['suppressed']['cooldown'] == 1
    third = select_notifications([item()], TrayPolicy(), first['new_state'], NOW + timedelta(minutes=120))
    assert len(third['selected']) == 1


@pytest.mark.parametrize('change', [
    {'title': '另一事项'}, {'date': '2026-04-01'}, {'severity': '中'}, {'message': '新建议'}
])
def test_changed_fingerprint_can_notify_during_cooldown(change):
    policy = TrayPolicy(min_severity='低')
    first = select_notifications([item()], policy, {}, NOW)
    changed = item(**change)
    assert select_notifications([changed], policy, first['new_state'], NOW + timedelta(minutes=1))['selected']


def test_invalid_inputs_counted_and_non_list_safe():
    result = select_notifications([None, {}, item(severity='bad')], TrayPolicy(), {}, NOW)
    assert result['suppressed']['invalid'] == 3
    assert select_notifications(None, TrayPolicy(), {}, NOW)['suppressed']['invalid'] == 1


def test_state_prunes_old_malformed_and_caps_at_1000():
    state = {item_key(item(str(i))): {'last_shown': (NOW - timedelta(minutes=i)).isoformat(),
                                     'item_fingerprint': str(i)} for i in range(1100)}
    state['old'] = {'last_shown': (NOW - timedelta(days=31)).isoformat(), 'item_fingerprint': 'x'}
    state['bad'] = {'last_shown': 'oops', 'item_fingerprint': 'x'}
    result = select_notifications([], TrayPolicy(), state, NOW)
    assert len(result['new_state']) == 1000 and 'old' not in result['new_state']


def test_state_is_copied_not_mutated():
    first = select_notifications([item()], TrayPolicy(), {}, NOW)
    state = first['new_state']
    snapshot = {k: dict(v) for k, v in state.items()}
    select_notifications([item('2')], TrayPolicy(), state, NOW + timedelta(minutes=1))
    assert state == snapshot


def test_notification_text_plain_bounded_and_no_evidence():
    value = item(title='<b>合同</b>', message='<i>' + '处理建议' * 100 + '</i>', evidence='秘密证据全文')
    text = notification_text(value)
    assert text['title'] == '高等级IT提醒 · 合同到期'
    assert '事项：合同' in text['message'] and '日期：2026-03-20' in text['message']
    assert len(text['message']) <= 240 and '<' not in text['message'] and '秘密' not in text['message']


def test_select_rejects_naive_now():
    with pytest.raises(ValueError, match='托盘扫描时间无效'):
        select_notifications([], TrayPolicy(), {}, NOW.replace(tzinfo=None))


# ---- Qt wrapper: all native tray behavior patched/offscreen (at least 8 cases) ----
def test_qt_unavailable_degrades_without_icon(qapp, settings):
    with patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=False):
        controller = TrayController(None, Mock(), settings)
    assert not controller.available and controller.icon is None


def make_controller(settings, callback=None):
    callback = callback or Mock()
    with patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=True), \
         patch.object(QSystemTrayIcon, 'show'):
        return TrayController(None, callback, settings), callback


def test_qt_default_disabled_does_not_start(qapp, settings):
    controller, _ = make_controller(settings)
    assert controller.start(lambda: [item()], 10) is False
    assert controller.timer is None
    controller.stop()


def test_qt_enable_starts_timer(qapp, settings):
    controller, _ = make_controller(settings)
    controller.update_policy(enabled=True)
    assert controller.start(lambda: [], 1234)
    assert controller.timer.isActive() and controller.timer.interval() == 1234
    controller.stop()


def test_qt_show_message_is_rate_limited(qapp, settings):
    controller, _ = make_controller(settings)
    controller.update_policy(enabled=True, quiet_start='00:00', quiet_end='00:01')
    controller.items_provider = lambda: [item()]
    with patch.object(controller.icon, 'showMessage') as shown:
        controller.check_now()
        controller.check_now()
    assert shown.call_count == 1
    controller.stop()


def test_qt_trigger_click_opens_center(qapp, settings):
    controller, callback = make_controller(settings)
    controller._activated(QSystemTrayIcon.ActivationReason.Trigger)
    callback.assert_called_once_with()
    controller.stop()


def test_qt_menu_exit_wording_is_explicit(qapp, settings):
    controller, _ = make_controller(settings)
    texts = [action.text() for action in controller.menu.actions()]
    assert '打开提醒中心' in texts and '立即检查' in texts and '启用提醒' in texts
    assert '退出桌面（不停止服务）' in texts
    controller.stop()


def test_qt_state_persists_and_corrupt_json_degrades(qapp, settings):
    settings.setValue('tray/state', '{broken')
    controller, _ = make_controller(settings)
    assert controller.state == {}
    controller.update_policy(enabled=True, quiet_start='00:00', quiet_end='00:01')
    controller.items_provider = lambda: [item()]
    with patch.object(controller.icon, 'showMessage'):
        controller.check_now()
    saved = settings.value('tray/state')
    assert 'tray-' in saved and 'last_shown' in saved
    controller.stop()


def test_qt_stop_clears_timer_and_icon(qapp, settings):
    controller, _ = make_controller(settings)
    controller.update_policy(enabled=True)
    controller.start(lambda: [], 1000)
    icon = controller.icon
    with patch.object(icon, 'hide') as hidden:
        controller.stop()
    hidden.assert_called_once_with()
    assert controller.timer is None and controller.icon is None


def test_qt_policy_update_persists(qapp, settings):
    controller, _ = make_controller(settings)
    controller.update_policy(enabled=True, min_severity='中', cooldown_minutes=30,
                             max_per_cycle=5, quiet_start='23:00', quiet_end='07:00')
    assert settings.value('tray/min_severity') == '中'
    assert int(settings.value('tray/cooldown_minutes')) == 30
    controller.stop()


def test_qt_provider_failure_is_safe(qapp, settings):
    controller, _ = make_controller(settings)
    controller.update_policy(enabled=True)
    controller.items_provider = Mock(side_effect=RuntimeError('private details'))
    with patch.object(controller.icon, 'showMessage') as shown:
        assert controller.check_now() is None
    shown.assert_not_called()
    controller.stop()
