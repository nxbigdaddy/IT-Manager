"""Theme, font scale, shortcuts and dashboard card preferences."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from unittest.mock import patch

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from itmanager.gui import MainWindow
from itmanager.prefs import DEFAULT_CARDS, Prefs
from itmanager.theme import build_qss, dark_stylesheet, scale_stylesheet
from itmanager.workspace_ui import QSS


class Client:
    url = 'http://127.0.0.1:18765'

    def get(self, path):
        return []

    def post(self, path, payload):
        return {}


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def prefs(tmp_path):
    settings = QSettings(str(tmp_path / 'prefs.ini'), QSettings.IniFormat)
    return Prefs(settings)


def test_prefs_defaults_and_clamping(prefs):
    assert prefs.theme == 'light'
    assert prefs.font_scale == 1.0
    assert prefs.dashboard_cards == DEFAULT_CARDS
    prefs.theme = 'nonsense'
    assert prefs.theme == 'light'
    prefs.font_scale = 9
    assert prefs.font_scale == 1.45
    prefs.font_scale = 0.1
    assert prefs.font_scale == 0.85
    assert prefs.toggle_theme() == 'dark'
    assert prefs.toggle_theme() == 'light'
    prefs.font_scale = 1.0
    assert prefs.bump_scale(0.1) == 1.1


def test_dashboard_cards_are_persisted_and_filtered(prefs):
    prefs.dashboard_cards = ['today', 'backup', 'unknown-card']
    assert prefs.dashboard_cards == ['today', 'backup']
    prefs.dashboard_cards = []
    assert prefs.dashboard_cards == DEFAULT_CARDS


def test_theme_stylesheets_keep_structure_and_scale():
    dark = build_qss(QSS, 'dark', 1.0)
    assert dark != QSS
    for selector in ('QComboBox::down-arrow', 'QComboBox QAbstractItemView::item:hover',
                     'QPushButton[primary="true"]', 'QHeaderView::section', 'QSplitter::handle'):
        assert selector in QSS and selector in dark, selector
    assert '#f5f7fb' not in dark                    # light background token replaced
    assert '#161b23' in dark
    scaled = build_qss(QSS, 'light', 1.3)
    assert 'font-size:17px' in scaled
    assert scaled == scale_stylesheet(QSS, 1.3)
    assert build_qss(QSS, 'light', 1.0) == QSS
    assert 'appTitleBar' in dark_stylesheet(QSS)


def test_window_exposes_theme_controls_and_shortcuts(qapp, prefs):
    window = MainWindow(Client(), autoload=False)
    window.prefs = prefs
    window.apply_theme()
    assert window.theme_button.text() == '深色模式'
    window.toggle_theme()
    assert window.prefs.theme == 'dark'
    assert window.theme_button.text() == '浅色模式'
    assert '#f5f7fb' not in window.styleSheet()
    window.change_scale(0.15)
    assert window.prefs.font_scale == 1.15
    window.reset_scale()
    assert window.prefs.font_scale == 1.0
    sequences = {shortcut.key().toString() for shortcut in window.shortcuts}
    for expected in ('F5', 'Ctrl+N', 'Ctrl+E', 'Ctrl+H', 'Ctrl+F', 'Ctrl+B', 'Ctrl+T'):
        assert expected in sequences
    window.toggle_theme()


def test_shortcuts_guard_when_nothing_is_selected(qapp, prefs):
    window = MainWindow(Client(), autoload=False)
    window.prefs = prefs
    messages = []
    window.statusBar().showMessage = messages.append
    window.navigate(window.keys.index('dashboard'))
    window.shortcut_new()
    window.shortcut_edit()
    window.shortcut_history()
    window.shortcut_batch()
    assert len(messages) == 4
    assert all(any(word in message for word in ('先', '不能', '不支持', '需要')) for message in messages)


def settle(qapp):
    """Flush deferred deletions so rebuilt pages do not leak stale widgets."""
    from PySide6.QtCore import QCoreApplication, QEvent
    for _ in range(3):
        qapp.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_dashboard_respects_card_preferences(qapp, prefs):
    window = MainWindow(Client(), autoload=False)
    window.prefs = prefs
    window.records = [{'id': 'T1', 'module': 'tasks', 'version': 1, 'archived': 0,
                       'data': {'名称': '演示任务', '状态': '未开始', '截止日期': '2030-01-01'}}]
    prefs.dashboard_cards = ['today']
    window.navigate(window.keys.index('dashboard'))
    settle(qapp)
    from PySide6.QtWidgets import QPushButton
    labels = [button.text() for button in window.page.findChildren(QPushButton)]
    assert any('今日任务' in text for text in labels)
    assert not any('待决策' in text for text in labels)
    prefs.dashboard_cards = DEFAULT_CARDS


def test_configure_dashboard_cards_dialog_applies_choice(qapp, prefs):
    window = MainWindow(Client(), autoload=False)
    window.prefs = prefs
    from PySide6.QtWidgets import QCheckBox, QDialog
    captured = {}

    def fake_exec(dialog):
        for box in dialog.findChildren(QCheckBox):
            captured[box.text()] = box.isChecked()
        checkboxes = dialog.findChildren(QCheckBox)
        for box in checkboxes:
            box.setChecked(box.text() == '备份健康')
        return QDialog.Accepted

    with patch.object(QDialog, 'exec', fake_exec):
        window.configure_dashboard_cards()
    assert prefs.dashboard_cards == ['backup']
    prefs.dashboard_cards = DEFAULT_CARDS
