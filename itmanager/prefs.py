"""Desktop preferences (theme, font scale, dashboard cards) stored with QSettings."""
from __future__ import annotations

from PySide6.QtCore import QSettings

ORGANISATION = 'ITManagement'
APPLICATION = 'Desktop'
THEMES = ('light', 'dark')
MIN_SCALE, MAX_SCALE = 0.85, 1.45
DEFAULT_CARDS = ['today', 'overdue', 'high_risk', 'pending_decision', 'expiring', 'reminders']
CARD_LABELS = {
    'today': '今日任务', 'overdue': '逾期任务', 'high_risk': '高风险',
    'pending_decision': '待决策', 'expiring': '30天内到期合同', 'workload': '人员负载',
    'backup': '备份健康', 'health': '体检发现', 'reminders': '待处理提醒',
}


class Prefs:
    """Small persisted preference set; never stores secrets or business data."""

    def __init__(self, settings=None):
        self.settings = settings or QSettings(ORGANISATION, APPLICATION)

    def get(self, key, default=None):
        value = self.settings.value(key, default)
        return default if value is None else value

    @property
    def theme(self):
        value = str(self.get('ui/theme', 'light'))
        return value if value in THEMES else 'light'

    @theme.setter
    def theme(self, value):
        self.settings.setValue('ui/theme', value if value in THEMES else 'light')

    @property
    def font_scale(self):
        try:
            value = float(self.get('ui/font_scale', 1.0))
        except (TypeError, ValueError):
            return 1.0
        return min(MAX_SCALE, max(MIN_SCALE, value))

    @font_scale.setter
    def font_scale(self, value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 1.0
        self.settings.setValue('ui/font_scale', min(MAX_SCALE, max(MIN_SCALE, value)))

    @property
    def dashboard_cards(self):
        value = self.get('ui/dashboard_cards', None)
        if value is None:
            return list(DEFAULT_CARDS)
        if isinstance(value, str):
            value = [item for item in value.split(',') if item]
        if not isinstance(value, (list, tuple)):
            return list(DEFAULT_CARDS)
        known = [item for item in value if item in CARD_LABELS]
        return known or list(DEFAULT_CARDS)

    @dashboard_cards.setter
    def dashboard_cards(self, values):
        known = [item for item in (values or []) if item in CARD_LABELS]
        self.settings.setValue('ui/dashboard_cards', ','.join(known or DEFAULT_CARDS))

    def toggle_theme(self):
        self.theme = 'dark' if self.theme == 'light' else 'light'
        return self.theme

    def bump_scale(self, delta):
        self.font_scale = round(self.font_scale + delta, 2)
        return self.font_scale
