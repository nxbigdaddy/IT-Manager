"""Local desktop tray reminder policy and a small Qt wrapper.

The selection helpers are deterministic, perform no IO/network access, and never
mutate their inputs.  ``TrayController`` only persists local QSettings and asks
Qt to display native tray messages.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QSettings, QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .prefs import APPLICATION, ORGANISATION

SAFE_TRAY_ERRORS = frozenset({
    '托盘提醒等级无效', '托盘提醒冷却时间无效', '托盘提醒数量无效',
    '托盘安静时段无效', '托盘提醒启用状态无效', '托盘扫描时间无效',
})
_SEVERITIES = ('高', '中', '低')
_RANK = {'低': 0, '中': 1, '高': 2}
_CLOCK = re.compile(r'(?:[01]\d|2[0-3]):[0-5]\d\Z')
_TAG = re.compile(r'<[^>]*>')
_STATE_DAYS = 30
_STATE_MAX = 1000


@dataclass(frozen=True)
class TrayPolicy:
    enabled: bool = False
    min_severity: str = '高'
    cooldown_minutes: int = 120
    max_per_cycle: int = 3
    quiet_start: str = '22:00'
    quiet_end: str = '08:00'

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError('托盘提醒启用状态无效')
        if self.min_severity not in _SEVERITIES:
            raise ValueError('托盘提醒等级无效')
        if type(self.cooldown_minutes) is not int or not 5 <= self.cooldown_minutes <= 1440:
            raise ValueError('托盘提醒冷却时间无效')
        if type(self.max_per_cycle) is not int or not 1 <= self.max_per_cycle <= 10:
            raise ValueError('托盘提醒数量无效')
        if not isinstance(self.quiet_start, str) or not _CLOCK.fullmatch(self.quiet_start):
            raise ValueError('托盘安静时段无效')
        if not isinstance(self.quiet_end, str) or not _CLOCK.fullmatch(self.quiet_end):
            raise ValueError('托盘安静时段无效')


def severity_rank(value) -> int:
    """Return increasing severity rank, or -1 for an unknown value."""
    return _RANK.get(value, -1)


def _aware(now):
    return isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None


def in_quiet_hours(policy: TrayPolicy, now: datetime) -> bool:
    """Whether an aware datetime falls in the half-open configured quiet span."""
    if not isinstance(policy, TrayPolicy) or not _aware(now):
        raise ValueError('托盘扫描时间无效')
    current = now.strftime('%H:%M')
    start, end = policy.quiet_start, policy.quiet_end
    if start == end:  # An equal boundary conventionally means quiet all day.
        return True
    return start <= current < end if start < end else current >= start or current < end


def item_key(item):
    """Return a stable readable key for a valid reminder item, otherwise None."""
    if not isinstance(item, dict):
        return None
    rule, record = item.get('rule_key'), item.get('record_id')
    if not isinstance(rule, str) or not rule.strip() or not isinstance(record, str) or not record.strip():
        return None
    payload = json.dumps([rule.strip(), record.strip()], ensure_ascii=False,
                         separators=(',', ':')).encode('utf-8')
    return 'tray-' + hashlib.sha256(payload).hexdigest()


def _fingerprint(item):
    # These are the user-visible facts whose changes should permit a fresh alert.
    facts = {key: item.get(key) for key in
             ('rule_key', 'record_id', 'rule_name', 'title', 'date', 'severity', 'message')}
    try:
        raw = json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                         default=str).encode('utf-8')
    except Exception:
        return None
    return hashlib.sha256(raw).hexdigest()


def _parse_shown(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if _aware(parsed) else None


def _pruned_state(state, now):
    cutoff = now - timedelta(days=_STATE_DAYS)
    kept = []
    if isinstance(state, dict):
        for key, value in state.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            shown = _parse_shown(value.get('last_shown'))
            fingerprint = value.get('item_fingerprint')
            if shown is None or not isinstance(fingerprint, str) or shown < cutoff:
                continue
            kept.append((shown, key, {'last_shown': shown.isoformat(),
                                      'item_fingerprint': fingerprint}))
    kept.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return {key: value for _shown, key, value in kept[:_STATE_MAX]}


def select_notifications(items, policy: TrayPolicy, state, now: datetime) -> dict:
    """Select native notifications and return an updated, bounded state snapshot."""
    if not isinstance(policy, TrayPolicy) or not _aware(now):
        raise ValueError('托盘扫描时间无效')
    new_state = _pruned_state(state, now)
    selected = []
    suppressed = {'quiet': 0, 'cooldown': 0, 'severity': 0, 'invalid': 0}
    quiet = in_quiet_hours(policy, now)
    source = items if isinstance(items, (list, tuple)) else ()
    if not isinstance(items, (list, tuple)):
        suppressed['invalid'] += 1
    for item in source:
        key = item_key(item)
        fingerprint = _fingerprint(item) if key else None
        severity = item.get('severity') if isinstance(item, dict) else None
        if key is None or fingerprint is None or severity_rank(severity) < 0:
            suppressed['invalid'] += 1
            continue
        if severity_rank(severity) < severity_rank(policy.min_severity):
            suppressed['severity'] += 1
            continue
        if quiet:
            suppressed['quiet'] += 1
            continue
        old = new_state.get(key)
        if old and old.get('item_fingerprint') == fingerprint:
            shown = _parse_shown(old.get('last_shown'))
            if shown is not None and now - shown < timedelta(minutes=policy.cooldown_minutes):
                suppressed['cooldown'] += 1
                continue
        if len(selected) >= policy.max_per_cycle:
            continue
        selected.append(dict(item))
        new_state[key] = {'last_shown': now.isoformat(), 'item_fingerprint': fingerprint}
    # Selection can push an already-full state over the cap.
    new_state = _pruned_state(new_state, now)
    return {'selected': selected, 'new_state': new_state, 'suppressed': suppressed}


def _plain(value):
    text = html.unescape(_TAG.sub('', value if isinstance(value, str) else ''))
    return ' '.join(text.replace('\x00', '').split())


def notification_text(item) -> dict:
    """Build short plain-text notification content without evidence details."""
    if not isinstance(item, dict):
        item = {}
    rule = _plain(item.get('rule_name')) or '提醒'
    title = ('%s等级IT提醒 · %s' % (_plain(item.get('severity')) or '高', rule))[:100]
    subject = _plain(item.get('title')) or '未命名事项'
    day = _plain(item.get('date')) or '日期待确认'
    advice = _plain(item.get('message')) or '请打开提醒中心核对并处理。'
    message = '事项：%s；日期：%s；建议：%s' % (subject, day, advice)
    if len(message) > 240:
        message = message[:239].rstrip('；，, ') + '…'
    return {'title': title, 'message': message}


class TrayController(QObject):
    """Own a local system-tray icon, timer and persisted notification state."""

    def __init__(self, parent, open_callback, settings=None):
        super().__init__(parent)
        self.settings = settings or QSettings(ORGANISATION, APPLICATION)
        self.open_callback = open_callback
        self.timer = None
        self.icon = None
        self.menu = None
        self.items_provider = None
        self.available = bool(QSystemTrayIcon.isSystemTrayAvailable())
        self.policy = self._load_policy()
        self.state = self._load_state()
        if self.available:
            self._build_tray(parent)

    def _value(self, key, default):
        value = self.settings.value('tray/' + key, default)
        return default if value is None else value

    @staticmethod
    def _bool(value):
        return value if type(value) is bool else str(value).lower() in ('1', 'true', 'yes')

    def _load_policy(self):
        try:
            return TrayPolicy(
                enabled=self._bool(self._value('enabled', False)),
                min_severity=str(self._value('min_severity', '高')),
                cooldown_minutes=int(self._value('cooldown_minutes', 120)),
                max_per_cycle=int(self._value('max_per_cycle', 3)),
                quiet_start=str(self._value('quiet_start', '22:00')),
                quiet_end=str(self._value('quiet_end', '08:00')),
            )
        except (TypeError, ValueError):
            return TrayPolicy()

    def _load_state(self):
        raw = self._value('state', '{}')
        try:
            value = json.loads(raw) if isinstance(raw, str) else {}
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _build_tray(self, parent):
        self.icon = QSystemTrayIcon(QIcon(), parent)
        self.menu = QMenu()
        self.open_action = self.menu.addAction('打开提醒中心')
        self.open_action.triggered.connect(self.open_callback)
        self.check_action = self.menu.addAction('立即检查')
        self.check_action.triggered.connect(self.check_now)
        self.enabled_action = QAction('启用提醒', self.menu, checkable=True)
        self.enabled_action.setChecked(self.policy.enabled)
        self.enabled_action.toggled.connect(self._toggle_enabled)
        self.menu.addAction(self.enabled_action)
        self.menu.addSeparator()
        self.exit_action = self.menu.addAction('退出桌面（不停止服务）')
        self.exit_action.triggered.connect(self._quit_desktop)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self.icon.show()

    def start(self, items_provider, interval_ms=60000):
        self.items_provider = items_provider
        if not self.available or not self.policy.enabled:
            return False
        if self.timer is None:
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.check_now)
        self.timer.start(max(1, int(interval_ms)))
        return True

    def _toggle_enabled(self, enabled):
        self.update_policy(enabled=bool(enabled))
        if enabled and self.items_provider is not None:
            self.start(self.items_provider)
        elif self.timer is not None:
            self.timer.stop()

    def update_policy(self, **changes):
        values = asdict(self.policy)
        values.update(changes)
        policy = TrayPolicy(**values)
        self.policy = policy
        for key, value in asdict(policy).items():
            self.settings.setValue('tray/' + key, value)
        if hasattr(self, 'enabled_action') and self.enabled_action.isChecked() != policy.enabled:
            self.enabled_action.blockSignals(True)
            self.enabled_action.setChecked(policy.enabled)
            self.enabled_action.blockSignals(False)
        if not policy.enabled and self.timer is not None:
            self.timer.stop()
        return policy

    def check_now(self):
        if not self.available or not self.policy.enabled or not callable(self.items_provider):
            return None
        try:
            items = self.items_provider()
        except Exception:
            return None
        now = datetime.now().astimezone()
        result = select_notifications(items, self.policy, self.state, now)
        self.state = result['new_state']
        self.settings.setValue('tray/state', json.dumps(self.state, ensure_ascii=False,
                                                        separators=(',', ':')))
        for item in result['selected']:
            text = notification_text(item)
            self.icon.showMessage(text['title'], text['message'])
        return result

    def _activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_callback()

    @staticmethod
    def _quit_desktop():
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def stop(self):
        if self.timer is not None:
            self.timer.stop()
            self.timer.deleteLater()
            self.timer = None
        if self.icon is not None:
            self.icon.hide()
            self.icon.deleteLater()
            self.icon = None
