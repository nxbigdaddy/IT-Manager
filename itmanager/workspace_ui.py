"""Presentation-only workspace; MainWindow owns navigation, persistence and workers.

Call setup_workspace after initializing client/runner/records/config/current/generation.
MainWindow must delegate table, fill, record_page and dashboard to this mixin.
"""
from __future__ import annotations

import html
import json
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import QDate, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QTextBrowser, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)
from .schema import MODULES, STATUS

CONFIG_KEYS = ['providers', 'channels', 'prompts', 'schedules']
RECORD_KEYS = ['ai_calls', 'deliveries', 'audit']
LABELS = {'workspaces':'专项工作空间', 'checklists':'日常检查', 'reminders':'提醒中心', 'health':'数据体检',
          'charts':'统计看板', 'dashboard': '工作台', 'drafts': '汇报与草稿', 'ai': 'AI工作室',
          'providers': '模型配置', 'channels': '通知渠道', 'prompts': '提示词库',
          'schedules': '周期计划', 'ai_calls': 'AI调用记录', 'deliveries': '发送记录',
          'audit': '审计记录', 'system': '系统与导入导出'}
GROUPS = [('日常工作', ['reminders','checklists','tasks', 'logs', 'incidents', 'decisions', 'risks']),
          ('项目规划', ['workspaces','plans', 'projects', 'requests', 'collection']),
          ('人员', ['people', 'reviews']),
          ('IT台账', ['systems', 'assets', 'access', 'contracts', 'budget', 'recovery', 'changes', 'metrics']),
          ('汇报AI', ['drafts', 'ai', 'charts']),
          ('系统设置', CONFIG_KEYS + RECORD_KEYS + ['system','health'])]
SERVER_PAGING_THRESHOLD = 1000
# Modules whose records may carry real evidence files; others keep text-only evidence.
EVIDENCE_MODULES = {'tasks', 'logs', 'collection', 'systems', 'assets', 'access', 'risks',
                    'requests', 'incidents', 'changes', 'contracts', 'budget', 'recovery',
                    'decisions', 'metrics', 'reviews', 'projects', 'plans'}
CLOSED = {'已完成', '已取消', '已关闭', '已定稿'}
QUICK_LABELS = {'today': '今日任务', 'overdue': '逾期任务', 'high_risk': '高风险',
                'pending_decision': '待决策', 'expiring': '30天内到期', 'open': '未结任务'}
# Intentionally bounded: long evidence and relationship identifiers belong in details.
COLUMNS = {
    'tasks': ['名称', '负责人', '优先级', '截止日期', '状态', '下一步'],
    'plans': ['名称', '周期', '负责人', '开始日期', '截止日期', '状态'],
    'projects': ['名称', '项目负责人', '阶段', '截止日期', '状态', '预算'],
    'people': ['姓名', '部门', '岗位', '主管', '邮箱', '状态'],
    'reviews': ['名称', '考核周期', '复核人', '评分', '状态'],
    'logs': ['名称', '日期', '工作内容', '结果', '下一步'],
    'collection': ['名称', '类别', '提供人', '截止日期', '核验状态', '缺口'],
    'systems': ['名称', '业务负责人', '技术负责人', '关键性', '供应商', '数据等级'],
    'assets': ['名称', '类型', '责任人', '位置', '维保到期日期', '状态'],
    'access': ['名称', '责任人', '账号类型', '复核日期', 'MFA状态', '结论'],
    'risks': ['名称', '等级', '负责人', '截止日期', '状态', '复核日期'],
    'requests': ['名称', '部门', '业务负责人', '优先级', '预计费用', '状态'],
    'incidents': ['名称', '级别', '负责人', '受理时间', '状态', '恢复时间'],
    'changes': ['名称', '执行人', '计划窗口', '批准人', '状态'],
    'contracts': ['名称', '供应商', '负责人', '金额', '到期日期', '状态'],
    'budget': ['名称', '年度', '负责人', '预算', '已发生', '财务确认'],
    'recovery': ['名称', '负责人', '演练日期', '实际耗时', '结论'],
    'decisions': ['名称', '提出人', '决策人', '截止日期', '结论', '状态'],
    'metrics': ['名称', '周期', '负责人', '目标', '实际', '单位'],
}
QSS = """
QMainWindow, QWidget { background:#f5f7fb; color:#243249; font-size:13px; }
QLabel { background:transparent; }
QTreeWidget, QTableWidget, QTextBrowser, QLineEdit, QComboBox, QDateEdit {
 background:white; border:1px solid #dde4ee; border-radius:6px; padding:5px;
 selection-background-color:#dceaff; selection-color:#173e72; }
QComboBox {
 background:#ffffff; color:#243249; border:1px solid #cfd9e6;
 border-radius:7px; min-height:22px; padding:6px 36px 6px 11px;
 selection-background-color:#e8f1ff; selection-color:#174d8e;
 combobox-popup:0;
}
QComboBox:hover { border-color:#8ba9cf; background:#fbfdff; }
QComboBox:focus { border-color:#3977c5; background:#ffffff; }
QComboBox:on { border-color:#3977c5; }
QComboBox:disabled { color:#94a3b8; background:#f0f3f7; border-color:#e0e6ee; }
QComboBox::drop-down {
 subcontrol-origin:padding; subcontrol-position:top right; width:30px;
 border:0; border-left:1px solid #e8edf4;
 border-top-right-radius:7px; border-bottom-right-radius:7px;
 background:transparent;
}
QComboBox::drop-down:hover { background:#edf4fe; }
QComboBox::down-arrow { image:url("__COMBO_ARROW__"); width:16px; height:16px; }
QComboBox QLineEdit { border:0; border-radius:0; background:transparent; padding:0; }
QComboBox QAbstractItemView {
 background:#ffffff; color:#243249; border:1px solid #cfdaea;
 border-radius:7px; padding:5px; outline:0;
 selection-background-color:#e8f1ff; selection-color:#174d8e;
}
QComboBox QAbstractItemView::item { min-height:30px; padding:3px 10px; border:0; }
QComboBox QAbstractItemView::item:hover { background:#f1f6fd; }
QComboBox QAbstractItemView::item:selected { background:#e8f1ff; color:#174d8e; }
QComboBox QAbstractItemView QScrollBar:vertical { background:#f4f7fb; width:8px; border:0; margin:0; }
QComboBox QAbstractItemView QScrollBar::handle:vertical { background:#b8c7da; min-height:24px; border-radius:4px; }
QComboBox QAbstractItemView QScrollBar::add-line:vertical,
QComboBox QAbstractItemView QScrollBar::sub-line:vertical { height:0; }
QTreeWidget { border:0; padding:8px; }
QTreeWidget::item { height:34px; padding-left:4px; }
QTreeWidget::item:selected { background:#dceaff; color:#1655a1; border-radius:5px; }
QPushButton { background:white; border:1px solid #d5deeb; border-radius:6px; padding:7px 12px; }
QPushButton:hover { background:#edf4ff; border-color:#86abe0; }
QPushButton:disabled { color:#9da7b5; background:#f0f2f6; }
QPushButton[primary="true"] { background:#2563b8; color:white; border:0; }
QPushButton[stat="true"] { text-align:left; background:white; padding:14px; font-size:15px; }
QHeaderView::section { background:#eef3fa; border:0; border-bottom:1px solid #dce3ee; padding:9px; font-weight:600; }
QTableWidget { gridline-color:#eef1f6; alternate-background-color:#fafcff; }
QSplitter::handle { background:#e7edf5; width:4px; }
QFrame[card="true"] { background:white; border:1px solid #dde4ee; border-radius:9px; }
""".replace('__COMBO_ARROW__', (Path(__file__).parent / 'resources' / 'chevron-down.svg').as_posix())


def _titlebar_style():
    from .window_chrome import titlebar_style
    return titlebar_style()


def _button(text, callback, layout, primary=False):
    result = QPushButton(text)
    result.setProperty('primary', primary)
    result.clicked.connect(callback)
    layout.addWidget(result)
    return result


class NavigationItem(QTreeWidgetItem):
    """QListWidgetItem-compatible no-argument text() for existing navigation."""
    def text(self, column=0):
        return super().text(column)


class GroupedNavigation(QTreeWidget):
    currentRowChanged = Signal(int)

    def __init__(self, keys, parent=None):
        super().__init__(parent)
        self.keys = list(keys)
        self._items = {}
        self.setHeaderHidden(True)
        self.setFixedWidth(230)
        self.setIndentation(14)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._add_leaf('dashboard', self)
        for label, keys_in_group in GROUPS:
            group = QTreeWidgetItem(self, [label])
            group.setFlags(group.flags() & ~Qt.ItemIsSelectable)
            for key in keys_in_group:
                if key in self.keys:
                    self._add_leaf(key, group)
            group.setExpanded(label == '日常工作')
        # Future schema modules remain addressable without changing the key order.
        for key in self.keys:
            if key not in self._items:
                self._add_leaf(key, self)
        self.currentItemChanged.connect(self._changed)

    def _add_leaf(self, key, parent):
        item = NavigationItem(parent, [MODULES[key][0] if key in MODULES else LABELS.get(key, key)])
        item.setData(0, Qt.UserRole, self.keys.index(key))
        self._items[key] = item

    def _changed(self, current, previous):
        if current is not None and current.data(0, Qt.UserRole) is not None:
            self.currentRowChanged.emit(current.data(0, Qt.UserRole))

    def setCurrentRow(self, index):
        item = self.item(index)
        if item is not None:
            if item.parent():
                item.parent().setExpanded(True)
            self.setCurrentItem(item)
            self.scrollToItem(item)

    def currentRow(self):
        item = self.currentItem()
        value = item.data(0, Qt.UserRole) if item else None
        return value if value is not None else -1

    def item(self, index):
        return self._items[self.keys[index]] if 0 <= index < len(self.keys) else None

    def count(self):
        return len(self.keys)


class WorkspaceMixin:
    def setup_workspace(self):
        self.view_state = getattr(self, 'view_state', {})
        self.quick_filter = getattr(self, 'quick_filter', None)
        self.setWindowTitle('IT推进管理 · 工作空间')
        self.setMinimumSize(1024, 700)
        self.resize(1350, 850)
        self.setFont(QFont('Microsoft YaHei UI', 10))
        from .prefs import Prefs
        self.prefs = getattr(self, 'prefs', None) or Prefs()
        self.apply_theme()
        root = QWidget()
        self.setCentralWidget(root)
        frame_layout = QVBoxLayout(root)
        frame_layout.setContentsMargins(0,0,0,0)
        frame_layout.setSpacing(0)
        from .window_chrome import install_chrome
        install_chrome(self,frame_layout)
        content=QWidget()
        frame_layout.addWidget(content,1)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 10, 12, 10)
        toolbar = QHBoxLayout()
        brand = QLabel('IT 推进管理')
        brand.setStyleSheet('font-size:19px;font-weight:700;color:#174d8e')
        toolbar.addWidget(brand)
        self.global_search = QLineEdit()
        self.global_search.setObjectName('global_search')
        self.global_search.setPlaceholderText('全局搜索任务、系统、资产、人员…（Ctrl+Shift+F）')
        self.global_search.setMaximumWidth(420)
        self.global_search.returnPressed.connect(self.open_global_search)
        toolbar.addWidget(self.global_search, 1)
        toolbar.addStretch()
        self.connection = QLabel('未连接')
        toolbar.addWidget(self.connection)
        for label, callback in [('刷新', self.refresh), ('连接设置', self.connect_settings), ('帮助', self.help)]:
            _button(label, callback, toolbar)
        self.theme_button = _button('深色模式', self.toggle_theme, toolbar)
        self.scale_down = _button('A-', lambda: self.change_scale(-0.05), toolbar)
        self.scale_up = _button('A+', lambda: self.change_scale(0.05), toolbar)
        self.theme_button.setToolTip('切换浅色 / 深色主题（Ctrl+T）')
        self.scale_down.setToolTip('缩小字体（Ctrl+-）')
        self.scale_up.setToolTip('放大字体（Ctrl+=）')
        self.tray_button = _button('托盘提醒', self.configure_tray_notifications, toolbar)
        self.tray_button.setToolTip('仅在桌面运行时显示本机托盘提醒；默认关闭，不发送外部消息')
        layout.addLayout(toolbar)
        body = QHBoxLayout()
        self.keys = ['dashboard'] + list(MODULES) + ['drafts', 'ai'] + CONFIG_KEYS + RECORD_KEYS + ['system','checklists','workspaces','reminders','health','charts']
        self.nav = GroupedNavigation(self.keys)
        body.addWidget(self.nav)
        self.page = QWidget()
        self.page_layout = QVBoxLayout(self.page)
        self.page_layout.setContentsMargins(14, 4, 2, 4)
        body.addWidget(self.page, 1)
        layout.addLayout(body, 1)
        self.nav.currentRowChanged.connect(self.navigate)
        self.install_shortcuts()
        self.nav.setCurrentRow(0)
        self.statusBar().showMessage('外部链接与发送均需确认 · 统计基于当前已刷新数据'
                                     ' · 快捷键：F5 刷新 / Ctrl+N 新建 / Ctrl+F 页内搜索 / Ctrl+Shift+F 全局搜索 / Ctrl+B 批量 / Ctrl+T 主题')

    # ------------------------------------------------------------------ 主题与快捷键
    def apply_theme(self):
        """Re-apply the stored theme and font scale to the whole window."""
        from .theme import build_qss
        base = QSS + '\n' + _titlebar_style()
        self.setStyleSheet(build_qss(base, self.prefs.theme, self.prefs.font_scale))
        if hasattr(self, 'theme_button'):
            self.theme_button.setText('浅色模式' if self.prefs.theme == 'dark' else '深色模式')

    def toggle_theme(self):
        self.prefs.toggle_theme()
        self.apply_theme()
        self.statusBar().showMessage('已切换到' + ('深色' if self.prefs.theme == 'dark' else '浅色') + '主题')

    def change_scale(self, delta):
        scale = self.prefs.bump_scale(delta)
        self.apply_theme()
        self.statusBar().showMessage(f'字体比例：{scale:.2f}（Ctrl+0 恢复默认）')

    def reset_scale(self):
        self.prefs.font_scale = 1.0
        self.apply_theme()
        self.statusBar().showMessage('字体比例已恢复 1.00')

    def install_shortcuts(self):
        """Keyboard shortcuts for the actions a single administrator repeats daily."""
        from PySide6.QtGui import QKeySequence, QShortcut
        bindings = [
            ('F5', self.refresh),
            ('Ctrl+R', self.refresh),
            ('Ctrl+N', self.shortcut_new),
            ('Ctrl+E', self.shortcut_edit),
            ('Ctrl+H', self.shortcut_history),
            ('Ctrl+F', self.shortcut_search),
            ('Ctrl+Shift+F', self.focus_global_search),
            ('Ctrl+B', self.shortcut_batch),
            ('Ctrl+1', lambda: self.jump_to('dashboard')),
            ('Ctrl+2', lambda: self.jump_to('remainders_page_key')),
            ('Ctrl+3', lambda: self.jump_to('health')),
            ('Ctrl+4', lambda: self.jump_to('charts')),
            ('Ctrl+,', self.connect_settings),
            ('Ctrl+T', self.toggle_theme),
            ('Ctrl+=', lambda: self.change_scale(0.05)),
            ('Ctrl+-', lambda: self.change_scale(-0.05)),
            ('Ctrl+0', self.reset_scale),
            ('Ctrl+D', self.configure_dashboard_cards),
        ]
        self.shortcuts = []
        for sequence, callback in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.WindowShortcut)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)

    def jump_to(self, key):
        if key == 'remainders_page_key':
            key = 'reminders'
        if key in self.keys:
            self.nav.setCurrentRow(self.keys.index(key))

    def _current_record_table(self):
        for table in self.page.findChildren(QTableWidget):
            item = table.item(table.currentRow(), 0)
            if item is not None and item.data(Qt.UserRole):
                return table
        return None

    def shortcut_new(self):
        if self.current in MODULES:
            self.edit_record(self.current)
        elif self.current == 'checklists' and hasattr(self, '_checklist_new'):
            self._checklist_new()
        else:
            self.statusBar().showMessage('当前页面不能新建记录；请先切换到业务模块。')

    def shortcut_edit(self):
        if self.current not in MODULES:
            self.statusBar().showMessage('当前页面不支持编辑记录。')
            return
        table = self._current_record_table()
        item = table.item(table.currentRow(), 0) if table is not None else None
        if item is None:
            self.statusBar().showMessage('请先选择一行记录。')
            return
        self.edit_record(self.current, item.data(Qt.UserRole))

    def shortcut_history(self):
        table = self._current_record_table()
        item = table.item(table.currentRow(), 0) if table is not None else None
        if item is None:
            self.statusBar().showMessage('请先选择一行记录再查看历史。')
            return
        self._workspace_history(item.data(Qt.UserRole))

    def shortcut_search(self):
        for widget in self.page.findChildren(QLineEdit):
            if widget.objectName() in ('workspace_search',) or '搜索' in widget.placeholderText():
                widget.setFocus()
                widget.selectAll()
                return
        self.statusBar().showMessage('当前页面没有搜索框。')

    def shortcut_batch(self):
        table = self._current_record_table()
        if table is None or self.current not in MODULES or not hasattr(self, 'batch_actions'):
            self.statusBar().showMessage('批量操作需要先进入业务列表并选择记录。')
            return
        self.batch_actions(self.current, table)

    def focus_global_search(self):
        if hasattr(self, 'global_search'):
            self.global_search.setFocus()
            self.global_search.selectAll()

    def open_global_search(self):
        from .global_search_ui import open_global_search
        text = self.global_search.text().strip() if hasattr(self, 'global_search') else ''
        dialog = open_global_search(self, text)
        if hasattr(self, 'global_search') and dialog is not None and dialog.result() == QDialog.Accepted:
            self.global_search.clear()

    def configure_tray_notifications(self):
        """Local-only system tray policy. Enabling it never sends robot/email messages."""
        from PySide6.QtWidgets import QDialogButtonBox, QSpinBox
        tray = getattr(self, 'tray', None)
        if tray is None or not tray.available:
            self.statusBar().showMessage('当前系统或离屏环境不支持系统托盘；提醒中心仍可正常使用。')
            return
        policy = tray.policy
        dialog = QDialog(self)
        dialog.setWindowTitle('本机托盘提醒')
        box = QVBoxLayout(dialog)
        box.addWidget(QLabel('托盘提醒仅在桌面程序运行时生效，不向群机器人或邮箱发送消息。'
                             '默认关闭；相同事项在冷却时间内不重复弹出，安静时段不提示。'))
        enabled = QCheckBox('启用本机托盘提醒')
        enabled.setChecked(policy.enabled)
        box.addWidget(enabled)
        row = QHBoxLayout(); box.addLayout(row)
        row.addWidget(QLabel('最低等级'))
        severity = QComboBox(); severity.addItems(['高','中','低']); severity.setCurrentText(policy.min_severity)
        row.addWidget(severity)
        row.addWidget(QLabel('冷却分钟'))
        cooldown = QSpinBox(); cooldown.setRange(5,1440); cooldown.setValue(policy.cooldown_minutes)
        row.addWidget(cooldown)
        row.addWidget(QLabel('每次最多'))
        maximum = QSpinBox(); maximum.setRange(1,10); maximum.setValue(policy.max_per_cycle)
        row.addWidget(maximum)
        quiet = QHBoxLayout(); box.addLayout(quiet)
        quiet.addWidget(QLabel('安静时段'))
        start = QLineEdit(policy.quiet_start); start.setPlaceholderText('22:00')
        end = QLineEdit(policy.quiet_end); end.setPlaceholderText('08:00')
        quiet.addWidget(start); quiet.addWidget(QLabel('至')); quiet.addWidget(end)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.addWidget(buttons); buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        if not dialog.exec():return
        try:
            tray.update_policy(enabled=enabled.isChecked(),min_severity=severity.currentText(),
                               cooldown_minutes=cooldown.value(),max_per_cycle=maximum.value(),
                               quiet_start=start.text().strip(),quiet_end=end.text().strip())
        except ValueError as exc:
            self.statusBar().showMessage(str(exc));return
        if enabled.isChecked():tray.start(self.tray_items);tray.check_now()
        self.statusBar().showMessage('托盘提醒已启用' if enabled.isChecked() else '托盘提醒已关闭')

    def configure_dashboard_cards(self):
        """Choose which workbench cards are shown; stored locally per user."""
        from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox
        from .prefs import CARD_LABELS, DEFAULT_CARDS
        current = self.prefs.dashboard_cards
        dialog = QDialog(self)
        dialog.setWindowTitle('工作台卡片')
        box = QVBoxLayout(dialog)
        box.addWidget(QLabel('勾选要在工作台首页显示的卡片；通知渠道未配置时相关卡片仍会显示数量。'))
        boxes = {}
        for key, label in CARD_LABELS.items():
            check = QCheckBox(label)
            check.setChecked(key in current)
            boxes[key] = check
            box.addWidget(check)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.addWidget(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec():
            chosen = [key for key, check in boxes.items() if check.isChecked()]
            self.prefs.dashboard_cards = chosen or list(DEFAULT_CARDS)
            if self.current == 'dashboard':
                self.navigate(self.nav.currentRow())
            self.statusBar().showMessage('工作台卡片已更新')

    def table(self, headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.ElideRight)
        table.setAlternatingRowColors(True)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(38)
        table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setDefaultSectionSize(120)
        header.setMinimumSectionSize(60)
        header.setMaximumSectionSize(420)
        header.setStretchLastSection(False)
        table.setSortingEnabled(True)
        self.page_layout.addWidget(table)
        return table

    def fill(self, table, rows, columns):
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, column in enumerate(columns):
                value = column(row)
                text = str(value if value is not None else '')
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, row)
                table.setItem(i, j, item)
        table.setSortingEnabled(True)

    def record_title(self, record):
        fields = MODULES.get(record.get('module'), ('', ['名称']))[1]
        return str(record.get('data', {}).get(fields[0]) or '未命名记录')

    def record_owner(self, record):
        data = record.get('data', {})
        values = [str(data[k]) for k in ('负责人', '项目负责人', '业务负责人', '技术负责人',
                  '责任人', '姓名', '执行人', '提供人', '提出人', '决策人', '复核人') if data.get(k)]
        if data.get('人员ID'):
            person = next((r for r in self.records if r.get('id') == data['人员ID']), None)
            if person:
                values.append(self.record_title(person))
        return ' / '.join(dict.fromkeys(values))

    def record_date(self, record):
        data = record.get('data', {})
        for field in ('截止日期', '日期', '到期日期', '维保到期日期', '复核日期', '演练日期',
                      '受理时间', '计划窗口', '开始日期', '采购日期'):
            raw = str(data.get(field) or '')[:10]
            try:
                return date.fromisoformat(raw).isoformat()
            except ValueError:
                continue
        return ''

    def matches_quick_filter(self, record, kind):
        data = record.get('data', {})
        today = date.today().isoformat()
        active = data.get('状态') not in CLOSED
        deadline = str(data.get('截止日期') or '')
        try:
            deadline = date.fromisoformat(deadline).isoformat()
        except ValueError:
            deadline = ''
        if kind == 'open':
            return record.get('module') == 'tasks' and active
        if kind in ('today', 'overdue'):
            return record.get('module') == 'tasks' and active and bool(deadline) and (deadline == today if kind == 'today' else deadline < today)
        if kind == 'high_risk':
            return record.get('module') == 'risks' and active and str(data.get('等级', '')).strip() in {'高', '高风险', '高危', '严重', '重大', '极高', 'high', 'High', 'critical', 'Critical'}
        if kind == 'pending_decision':
            return record.get('module') == 'decisions' and active and not str(data.get('结论') or '').strip()
        if kind == 'expiring':
            try:
                expiry = date.fromisoformat(str(data.get('到期日期', '')))
            except ValueError:
                return False
            return record.get('module') == 'contracts' and active and date.today() <= expiry <= date.today() + timedelta(days=30)
        return True

    def open_workspace_filter(self, module, kind):
        self.quick_filter = {'module': module, 'kind': kind}
        index = self.keys.index(module)
        if self.nav.currentRow() == index:
            self.navigate(index)
        else:
            self.nav.setCurrentRow(index)

    def _detail_html(self, record):
        def safe(value):
            text = str(value if value is not None else '')
            # Escape everything first; only explicit http(s) substrings become links.
            parts, cursor = [], 0
            for match in re.finditer(r'https?://[^\s<>\"\']+', text):
                parts.append(html.escape(text[cursor:match.start()]))
                url = html.escape(match.group(), quote=True)
                parts.append(f'<a href="{url}">{url}</a>')
                cursor = match.end()
            parts.append(html.escape(text[cursor:]))
            return ''.join(parts).replace('\n', '<br>')
        body = ''.join(f'<p><b>{html.escape(str(k))}</b><br>{safe(v)}</p>' for k, v in record.get('data', {}).items())
        return f'<h3>{html.escape(self.record_title(record))}</h3>{body}<hr><p>版本：{safe(record.get("version", ""))}<br>更新时间：{safe(record.get("updated", ""))}</p>'

    def _open_detail_link(self, url):
        if url.scheme().lower() not in ('http', 'https') or not url.host() or not url.isValid():
            self.statusBar().showMessage('仅允许经确认打开 HTTP / HTTPS 链接；本地文件不会直接打开。')
            return
        if QMessageBox.question(self, '打开外部链接', '将在浏览器打开：\n' + url.toString(),
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            QDesktopServices.openUrl(url)

    def open_record_attachments(self, record):
        """Evidence attachment manager for one saved record."""
        from .attachments_ui import open_attachments_dialog
        return open_attachments_dialog(self, record)

    def open_lifecycle_chain(self, record):
        """Read-only 事件→根因→变更→验证→复盘 chain for one record."""
        from .lifecycle_ui import open_lifecycle_dialog
        return open_lifecycle_dialog(self, record)

    def _workspace_history(self, row):
        if not row:
            return
        callback = getattr(self, 'history', None)
        if callable(callback):
            callback(row)
            return
        from .gui import show_data
        client = self.client
        self.run(lambda: client.get('/records/' + row['id'] + '/history'),
                 lambda value: show_data(self, '记录历史', value))

    def server_record_page(self, module):
        """Large ledgers: filter and slice on the service so the client stays fast."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        self.page_layout.addWidget(container, 1)
        layout.addWidget(QLabel(f'记录较多（已加载 {len(self.records)} 条），本页改为服务端检索与分页：'
                                '筛选条件发送到服务，只回传当前页。工作台统计仍基于最近一次完整刷新。'))
        tools = QHBoxLayout()
        layout.addLayout(tools)
        _button('新建', lambda: self.edit_record(module), tools, True)
        edit = _button('编辑', lambda: self._server_action(self.edit_record, module), tools)
        archive = _button('归档', lambda: self._server_action(self.archive), tools)
        history = _button('历史', lambda: self._server_action(self._workspace_history), tools)
        if hasattr(self, 'batch_actions'):
            _button('批量操作…', lambda: self.batch_actions(module, table), tools)
        tools.addStretch()
        search = QLineEdit()
        search.setPlaceholderText('服务端搜索所有字段（回车执行）')
        owner = QLineEdit()
        owner.setPlaceholderText('责任人 / 姓名')
        status = QComboBox()
        status.addItem('全部状态', '')
        for name in STATUS:
            status.addItem(name, name)
        filters = QHBoxLayout()
        layout.addLayout(filters)
        filters.addWidget(search, 2)
        filters.addWidget(owner, 1)
        filters.addWidget(status, 1)
        _button('查询', lambda: run_query(0), filters)
        splitter = QSplitter()
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)
        columns = COLUMNS.get(module, [k for k in MODULES[module][1] if not k.endswith('ID')][:6])
        table = self.table(columns)
        self.page_layout.removeWidget(table)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        splitter.addWidget(table)
        details = QTextBrowser()
        details.setOpenExternalLinks(False)
        details.anchorClicked.connect(self._open_detail_link)
        splitter.addWidget(details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([720, 280])
        pager = QHBoxLayout()
        layout.addLayout(pager)
        page_label = QLabel('尚未查询')
        pager.addWidget(page_label)
        pager.addStretch()
        previous = _button('上一页', lambda: run_query(max(0, state['offset'] - state['limit'])), pager)
        following = _button('下一页', lambda: run_query(state['offset'] + state['limit']), pager)
        state = {'offset': 0, 'limit': 100, 'total': 0, 'busy': False}
        generation = self.generation
        client = self.client

        def selected():
            item = table.item(table.currentRow(), 0)
            return item.data(Qt.UserRole) if item else None

        def selection_changed():
            row = selected()
            for widget in (edit, archive, history):
                widget.setEnabled(bool(row))
            details.setHtml(self._detail_html(row) if row else
                            '<h3>记录详情</h3><p>选择左侧记录查看完整内容；双击可编辑。</p>')

        def render(page):
            state.update(offset=page.get('offset', 0), total=page.get('total', 0),
                         limit=page.get('limit', state['limit']), busy=False)
            rows = page.get('records', [])
            self.fill(table, rows, [lambda r: self.record_title(r), self.record_owner,
                                    self.record_date][:len(columns)])
            first = state['offset'] + 1 if state['total'] else 0
            last = min(state['offset'] + len(rows), state['total'])
            page_label.setText(f'第 {first}—{last} 条 / 共 {state["total"]} 条')
            previous.setEnabled(state['offset'] > 0)
            following.setEnabled(bool(page.get('has_more')))
            selection_changed()

        def failed(message):
            state['busy'] = False
            previous.setEnabled(True)
            following.setEnabled(True)
            self.statusBar().showMessage('查询失败：' + str(message))

        def run_query(offset):
            if state['busy']:
                return
            state.update(offset=max(0, offset), busy=True)
            previous.setEnabled(False)
            following.setEnabled(False)
            params = {'module': module, 'limit': state['limit'], 'offset': state['offset']}
            for key, widget in (('q', search), ('owner', owner)):
                if widget.text().strip():
                    params[key] = widget.text().strip()
            if status.currentData():
                params['status'] = status.currentData()
            query = '&'.join(f'{key}={quote(str(value), safe="")}' for key, value in params.items())

            def work():
                return client.get('/records/page?' + query)

            def done(page):
                if generation == self.generation and client is self.client and isinstance(page, dict):
                    render(page)

            self.runner.submit(work, done, failed)

        table.itemSelectionChanged.connect(selection_changed)
        table.doubleClicked.connect(lambda _: self._server_action(self.edit_record, module))
        search.returnPressed.connect(lambda: run_query(0))
        owner.returnPressed.connect(lambda: run_query(0))
        status.currentIndexChanged.connect(lambda _: run_query(0))
        run_query(0)

    def _server_action(self, callback, *args):
        """Act on the selected row of the currently visible (possibly paged) table."""
        item = self.sender()
        row = None
        for table in self.page.findChildren(QTableWidget):
            current = table.item(table.currentRow(), 0)
            if current is not None and current.data(Qt.UserRole):
                row = current.data(Qt.UserRole)
                break
        if row is None:
            self.statusBar().showMessage('请先选择一行记录。')
            return
        if args:
            callback(*args, row)
        else:
            callback(row)

    def record_page(self):
        """Dispatch: small ledgers filter locally, large ledgers page on the service."""
        module = self.current
        if len(self.records) > SERVER_PAGING_THRESHOLD:
            return self.server_record_page(module)
        return self.record_page_local(module)

    def record_page_local(self, module):
        module = module or self.current
        state = self.view_state.setdefault(module, {})
        quick = self.quick_filter
        if isinstance(quick, dict) and quick.get('module') == module:
            state.clear()
            state['quick'] = quick.get('kind', '')
            self.quick_filter = None
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        self.page_layout.addWidget(container, 1)
        tools = QHBoxLayout()
        layout.addLayout(tools)
        _button('新建', lambda: self.edit_record(module), tools, True)
        edit = _button('编辑', lambda: selected_action(self.edit_record, module), tools)
        archive = _button('归档', lambda: selected_action(self.archive), tools)
        history = _button('历史', lambda: selected_action(self._workspace_history), tools)
        extra_actions=[]
        if hasattr(self,'related_records_dialog'):
            extra_actions.append(_button('关联工作',lambda:selected_action(self.related_records_dialog),tools))
        if module=='tasks' and hasattr(self,'task_action_dialog'):
            extra_actions.append(_button('推进任务',lambda:selected_action(self.task_action_dialog),tools,True))
        if hasattr(self,'create_action_dialog'):
            def create_followup():
                row=selected()
                if row:self.create_action_dialog(row['id'],['跟进：'+self.record_title(row)])
            extra_actions.append(_button('建立后续任务',create_followup,tools))
        if hasattr(self,'batch_actions'):
            extra_actions.append(_button('批量操作…',lambda:self.batch_actions(module,table),tools))
        if module in EVIDENCE_MODULES:
            extra_actions.append(_button('证据附件…',lambda:selected_action(self.open_record_attachments),tools))
        extra_actions.append(_button('关联链条…',lambda:selected_action(self.open_lifecycle_chain),tools))
        tools.addStretch()
        filters = QHBoxLayout()
        layout.addLayout(filters)
        search = QLineEdit(state.get('search', ''))
        search.setObjectName('workspace_search')
        search.setPlaceholderText('搜索所有字段')
        owner = QLineEdit(state.get('owner', ''))
        owner.setObjectName('workspace_owner')
        owner.setPlaceholderText('责任人 / 姓名')
        status = QComboBox()
        status.setObjectName('workspace_status')
        status_field = '核验状态' if module == 'collection' else ('状态' if '状态' in MODULES[module][1] else '结论')
        statuses = list(dict.fromkeys(STATUS + [str(r.get('data', {}).get(status_field)) for r in self.records if r.get('module') == module and r.get('data', {}).get(status_field)]))
        status.addItems(['全部状态'] + statuses)
        status.setCurrentText(state.get('status', '全部状态'))
        filters.addWidget(search, 2)
        filters.addWidget(owner, 1)
        filters.addWidget(status, 1)
        dates = QHBoxLayout()
        layout.addLayout(dates)
        dates.addWidget(QLabel('业务日期'))
        date_controls = []
        for key, label in [('start', '从'), ('end', '至')]:
            enabled = QCheckBox(label)
            calendar = QDateEdit()
            calendar.setObjectName('workspace_' + key)
            calendar.setCalendarPopup(True)
            calendar.setDisplayFormat('yyyy-MM-dd')
            calendar.setDate(QDate.fromString(state[key], 'yyyy-MM-dd') if state.get(key) else QDate.currentDate())
            enabled.setChecked(bool(state.get(key)))
            calendar.setEnabled(enabled.isChecked())
            enabled.toggled.connect(calendar.setEnabled)
            dates.addWidget(enabled)
            dates.addWidget(calendar)
            date_controls.append((key, enabled, calendar))
        quick_label = QLabel(QUICK_LABELS.get(state.get('quick'), ''))
        dates.addWidget(quick_label)
        dates.addStretch()
        reset = _button('清空筛选', lambda: clear_filters(), dates)
        splitter = QSplitter()
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)
        columns = COLUMNS.get(module, [k for k in MODULES[module][1] if not k.endswith('ID')][:6])
        table = self.table(columns)
        self.page_layout.removeWidget(table)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        splitter.addWidget(table)
        details = QTextBrowser()
        details.setObjectName('workspace_details')
        details.setMinimumWidth(210)
        details.setOpenLinks(False)
        details.setOpenExternalLinks(False)
        details.anchorClicked.connect(self._open_detail_link)
        splitter.addWidget(details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([720, 280])
        pager = QHBoxLayout()
        layout.addLayout(pager)
        page_label = QLabel()
        pager.addWidget(page_label)
        pager.addStretch()
        previous = _button('上一页', lambda: move(-1), pager)
        following = _button('下一页', lambda: move(1), pager)
        busy = [False]
        total = [0]

        def selected():
            item = table.item(table.currentRow(), 0)
            return item.data(Qt.UserRole) if item else None

        def selected_action(callback, *args):
            row = selected()
            if row:
                callback(*args, row)

        def selection_changed():
            if busy[0]:
                return
            row = selected()
            state['selected'] = row.get('id') if row else None
            for widget in (edit, archive, history, *extra_actions):
                widget.setEnabled(bool(row))
            details.setHtml(self._detail_html(row) if row else '<h3>记录详情</h3><p>选择左侧记录查看完整内容；双击可编辑。</p>')

        def filter_rows(reset_page=False):
            selected_id = state.get('selected')
            state.update(search=search.text(), owner=owner.text(), status=status.currentText())
            for key, enabled, calendar in date_controls:
                state[key] = calendar.date().toString('yyyy-MM-dd') if enabled.isChecked() else ''
            if reset_page:
                state['page'] = 0
            rows = [r for r in self.records if r.get('module') == module]
            rows = [r for r in rows if state['search'].casefold() in json.dumps(r.get('data', {}), ensure_ascii=False).casefold()
                    and state['owner'].casefold() in self.record_owner(r).casefold()
                    and (state['status'] == '全部状态' or str(r.get('data', {}).get(status_field, '')) == state['status'])
                    and (not state['start'] or self.record_date(r) >= state['start'])
                    and (not state['end'] or bool(self.record_date(r)) and self.record_date(r) <= state['end'])
                    and self.matches_quick_filter(r, state.get('quick'))]
            total[0] = len(rows)
            last = max(0, (len(rows) - 1) // 100)
            state['page'] = min(max(0, state.get('page', 0)), last)
            busy[0] = True
            table.clearSelection()
            table.setCurrentCell(-1, -1)
            self.fill(table, rows[state['page'] * 100:(state['page'] + 1) * 100],
                      [lambda r, field=field: r.get('data', {}).get(field, '') for field in columns])
            for i in range(table.rowCount()):
                if table.item(i, 0).data(Qt.UserRole).get('id') == selected_id:
                    table.selectRow(i)
                    break
            busy[0] = False
            selection_changed()
            previous.setEnabled(state['page'] > 0)
            following.setEnabled(state['page'] < last)
            page_label.setText(f'共 {len(rows)} 条 · 第 {state["page"] + 1} / {last + 1} 页 · 每页 100 条' + (' · 暂无匹配记录' if not rows else ''))

        def move(delta):
            state['page'] = state.get('page', 0) + delta
            filter_rows()

        def clear_filters():
            controls = [search, owner, status] + [w for _, check, calendar in date_controls for w in (check, calendar)]
            for widget in controls:
                widget.blockSignals(True)
            search.clear()
            owner.clear()
            status.setCurrentIndex(0)
            for _, enabled, calendar in date_controls:
                enabled.setChecked(False)
                calendar.setEnabled(False)
            state['quick'] = ''
            quick_label.clear()
            for widget in controls:
                widget.blockSignals(False)
            filter_rows(True)

        search.textChanged.connect(lambda _: filter_rows(True))
        owner.textChanged.connect(lambda _: filter_rows(True))
        status.currentIndexChanged.connect(lambda _: filter_rows(True))
        for _, enabled, calendar in date_controls:
            enabled.toggled.connect(lambda _: filter_rows(True))
            calendar.dateChanged.connect(lambda _: filter_rows(True))
        table.itemSelectionChanged.connect(selection_changed)
        table.doubleClicked.connect(lambda _: selected_action(self.edit_record, module))
        filter_rows()

    def extra_dashboard_cards(self):
        """Optional workbench cards: (key, (label, count, callback)); each is optional."""
        cards = []
        try:
            from .reminders import compute_items, default_rules
            rules = getattr(self, 'reminder_rules', None) or getattr(self, '_reminders_rules', None) or default_rules()
            items = compute_items(self.records, [rule for rule in rules if rule.get('enabled', True)], date.today())
            cards.append(('reminders', ('待处理提醒', len(items),
                                        lambda: self.jump_to('reminders'))))
        except Exception:
            pass
        try:
            from .health import scan
            findings = scan(self.records).get('findings', [])
            cards.append(('health', ('体检发现', len(findings), lambda: self.jump_to('health'))))
        except Exception:
            pass
        open_tasks = [r for r in self.records
                      if r.get('module') == 'tasks' and str(r.get('data', {}).get('状态')) not in CLOSED]
        owners = {}
        for row in open_tasks:
            owner = self.record_owner(row)
            for name in [part.strip() for part in str(owner).split('/') if part.strip()]:
                owners[name] = owners.get(name, 0) + 1
        top = sorted(owners.items(), key=lambda item: (-item[1], item[0]))[:1]
        cards.append(('workload', ('最高负载负责人' + (f'：{top[0][0]}' if top else ''), top[0][1] if top else 0,
                                   lambda: self.jump_to('tasks'))))
        return cards

    def dashboard_reminders(self):
        """(items, from_server) for the dashboard banner; None when the service is unavailable."""
        try:
            from .reminders import compute_items, default_rules
        except ImportError:
            return None
        rules = getattr(self, 'reminder_rules', None) or getattr(self, '_reminders_rules', None)
        from_server = bool(rules)
        rules = rules or default_rules()
        try:
            items = compute_items(self.records, [rule for rule in rules if rule.get('enabled', True)], date.today())
        except Exception:
            return None
        return items, from_server

    def dashboard(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        self.page_layout.addWidget(container, 1)
        welcome = QLabel('今天，把重要的事推进一步')
        welcome.setStyleSheet('font-size:21px;font-weight:600;padding:4px 0')
        layout.addWidget(welcome)
        layout.addWidget(QLabel(date.today().strftime('%Y年%m月%d日') + ' · 基于当前已刷新数据，点击卡片查看精确筛选结果'))
        grid = QGridLayout()
        layout.addLayout(grid)
        stats = [('today', '今日任务', 'tasks', 'today'), ('overdue', '逾期任务', 'tasks', 'overdue'),
                 ('high_risk', '高风险', 'risks', 'high_risk'),
                 ('pending_decision', '待决策', 'decisions', 'pending_decision'),
                 ('expiring', '30天内到期合同', 'contracts', 'expiring')]
        chosen = set(self.prefs.dashboard_cards)
        cards = QHBoxLayout()
        layout.addLayout(cards)
        cards.addWidget(QLabel('快捷操作'))
        _button('＋ 新任务', lambda: self.edit_record('tasks'), cards, True)
        _button('记工作日志', lambda: self.edit_record('logs'), cards)
        _button('准备报告', lambda: self.nav.setCurrentRow(self.keys.index('drafts')), cards)
        _button('配置卡片…', self.configure_dashboard_cards, cards)
        cards.addStretch()
        visible = [(name, label, module, kind) for name, label, module, kind in stats if name in chosen]
        for i, (name, label, module, kind) in enumerate(visible):
            count = sum(self.matches_quick_filter(r, kind) for r in self.records)
            card = QPushButton(f'{count}\n{label}  →')
            card.setProperty('stat', True)
            card.setMinimumHeight(78)
            card.clicked.connect(lambda checked=False, m=module, k=kind: self.open_workspace_filter(m, k))
            grid.addWidget(card, i // 3, i % 3)
        if not visible:
            layout.addWidget(QLabel('工作台卡片已全部关闭；点击“配置卡片…”重新选择。'))
        for name, extra in self.extra_dashboard_cards():
            if name in chosen:
                label, count, callback = extra
                card = QPushButton(f'{count}\n{label}  →')
                card.setProperty('stat', True)
                card.setMinimumHeight(56)
                card.clicked.connect(lambda checked=False, cb=callback: cb())
                layout.addWidget(card)
        reminder_preview = self.dashboard_reminders() if 'reminders' in chosen else None
        if reminder_preview is not None:
            reminder_items, rules_loaded = reminder_preview
            high = sum(1 for item in reminder_items if str(item.get('severity')) == '高')
            suffix = '' if rules_loaded else '（按内置默认规则估算，打开提醒中心可加载自定义规则）'
            banner = QPushButton(f'待处理提醒 {len(reminder_items)} 项（高 {high} 项）· 合同维保、权限复核、逾期任务  → 打开提醒中心{suffix}')
            banner.setProperty('stat', True)
            banner.setMinimumHeight(56)
            banner.clicked.connect(lambda checked=False, k='reminders': self.nav.setCurrentRow(self.keys.index(k)))
            layout.addWidget(banner)
        if hasattr(self, 'dashboard_charts'):
            try:
                charts = self.dashboard_charts()
            except Exception:
                charts = None
            if charts is not None:
                layout.addWidget(charts)
        if not self.records:
            guide = QLabel('从第一条任务开始\n明确负责人和截止日期，再记录工作进展；需要汇报时，进入「汇报与草稿」整理成果。\n已有数据？在「系统设置 → 系统与导入导出」导入，或点击顶部刷新。')
            guide.setWordWrap(True)
            guide.setStyleSheet('background:#eaf2fd;border-radius:8px;padding:18px;line-height:1.6')
            layout.addWidget(guide)
        lists = QHBoxLayout()
        layout.addLayout(lists, 1)
        for title, kind in [('今日待办', 'today'), ('逾期待推进', 'overdue')]:
            panel = QFrame()
            panel.setProperty('card', True)
            panel_layout = QVBoxLayout(panel)
            rows = [r for r in self.records if self.matches_quick_filter(r, kind)]
            rows.sort(key=lambda r: (self.record_date(r), self.record_title(r)))
            heading = QHBoxLayout()
            heading.addWidget(QLabel(f'{title} · {len(rows)}'))
            heading.addStretch()
            _button('查看全部', lambda checked=False, k=kind: self.open_workspace_filter('tasks', k), heading)
            panel_layout.addLayout(heading)
            table = self.table(['任务', '负责人', '截止日期'])
            self.page_layout.removeWidget(table)
            panel_layout.addWidget(table)
            self.fill(table, rows[:100], [self.record_title, self.record_owner, self.record_date])
            def edit_task(index, widget=table):
                item = widget.item(index.row(), 0)
                if item:
                    self.edit_record('tasks', item.data(Qt.UserRole))
            table.doubleClicked.connect(edit_task)
            if not rows:
                panel_layout.addWidget(QLabel('暂无待办，继续保持。' if kind == 'today' else '没有逾期任务，进展良好。'))
            elif len(rows) > 100:
                panel_layout.addWidget(QLabel('展示前100条，点击「查看全部」分页浏览。'))
            lists.addWidget(panel, 1)
