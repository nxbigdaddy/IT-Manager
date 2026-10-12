"""Offline workspace tests; deliberately independent of MainWindow wiring."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from datetime import date, timedelta
from unittest.mock import patch

try:
    from PySide6.QtCore import QUrl, Qt
    from PySide6.QtWidgets import QApplication, QMainWindow, QTableWidget, QPushButton, QLineEdit, QTextBrowser, QDateEdit
    from itmanager.workspace_ui import WorkspaceMixin, COLUMNS, CONFIG_KEYS, RECORD_KEYS
    from itmanager.schema import MODULES
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


def row(identifier, module='tasks', **data):
    return {'id': str(identifier), 'module': module, 'version': 1, 'data': {'名称': str(identifier), **data}}


if QT_AVAILABLE:
    class Harness(WorkspaceMixin, QMainWindow):
        def __init__(self, records=()):
            super().__init__()
            self.records = list(records)
            self.config = {}
            self.current = 'dashboard'
            self.generation = 0
            self.calls = []
            self.setup_workspace()

        def refresh(self):
            self.navigate(self.nav.currentRow())

        def connect_settings(self):
            pass

        def help(self):
            pass

        def navigate(self, index):
            self.current = self.keys[index]
            self.generation += 1
            while self.page_layout.count():
                item = self.page_layout.takeAt(0)
                widget = item.widget()
                if widget:
                    widget.setParent(None)
                    widget.deleteLater()
            if self.current == 'dashboard':
                self.dashboard()
            elif self.current in MODULES:
                self.record_page()

        def edit_record(self, *args):
            self.calls.append(('edit', args))

        def archive(self, record):
            self.calls.append(('archive', record))

        def history(self, record):
            self.calls.append(('history', record))


@unittest.skipUnless(QT_AVAILABLE, 'PySide6 not installed')
class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = Harness()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def page(self, module='tasks'):
        self.window.nav.setCurrentRow(self.window.keys.index(module))
        return self.window.page.findChild(QTableWidget)

    def button(self, text):
        return next(w for w in self.window.page.findChildren(QPushButton) if w.text() == text)

    def test_navigation_preserves_legacy_keys_and_signal(self):
        w = self.window
        self.assertEqual(w.keys, ['dashboard'] + list(MODULES) + ['drafts', 'ai'] + CONFIG_KEYS + RECORD_KEYS + ['system','checklists','workspaces','reminders','health','charts'])
        changes = []
        w.nav.currentRowChanged.connect(changes.append)
        for i, key in enumerate(w.keys):
            w.nav.setCurrentRow(i)
            self.assertEqual(w.nav.currentRow(), i)
            self.assertTrue(w.nav.item(i).text())
        self.assertEqual(changes[-1], len(w.keys) - 1)
        self.assertEqual(w.nav.width(), 230)
        self.assertGreaterEqual(w.minimumWidth(), 1024)
        self.assertGreaterEqual(w.minimumHeight(), 700)

    def test_initial_secondary_groups_collapsed(self):
        nav = self.window.nav
        for i in range(1, nav.topLevelItemCount()):
            group = nav.topLevelItem(i)
            self.assertEqual(group.isExpanded(), group.text(0) == '日常工作')

    def test_all_modules_have_bounded_columns_without_ids(self):
        self.assertEqual(set(COLUMNS), set(MODULES))
        for module in MODULES:
            table = self.page(module)
            self.assertLessEqual(table.columnCount(), 6)
            self.assertTrue(all(not table.horizontalHeaderItem(i).text().endswith('ID') for i in range(table.columnCount())))
            self.assertFalse(table.wordWrap())
            self.assertEqual(table.verticalHeader().defaultSectionSize(), 38)

    def test_paging_selection_actions_and_refresh_state(self):
        self.window.records = [row(i, 负责人='张三', 状态='进行中', 截止日期=date.today().isoformat()) for i in range(205)]
        table = self.page()
        self.assertEqual(table.rowCount(), 100)
        for name in ('编辑', '归档', '历史'):
            self.assertFalse(self.button(name).isEnabled())
        self.button('下一页').click()
        self.assertEqual(self.window.view_state['tasks']['page'], 1)
        table.selectRow(2)
        identifier = table.item(2, 0).data(Qt.UserRole)['id']
        self.button('编辑').click()
        self.assertEqual(self.window.calls[-1][1][1]['id'], identifier)
        self.window.refresh()
        table = self.window.page.findChild(QTableWidget)
        self.assertEqual(table.item(table.currentRow(), 0).data(Qt.UserRole)['id'], identifier)
        self.assertEqual(self.window.view_state['tasks']['page'], 1)
        owner = self.window.page.findChild(QLineEdit, 'workspace_owner')
        owner.setText('无人')
        self.assertEqual(table.rowCount(), 0)
        self.assertEqual(self.window.view_state['tasks']['page'], 0)
        self.assertFalse(self.button('编辑').isEnabled())
        self.window.refresh()
        self.assertEqual(self.window.page.findChild(QLineEdit, 'workspace_owner').text(), '无人')
        self.button('清空筛选').click()
        self.assertEqual(self.window.page.findChild(QTableWidget).rowCount(), 100)

    def test_precise_drilldowns_and_reset(self):
        today = date.today()
        self.window.records = [
            row('today', 截止日期=today.isoformat()),
            row('old', 截止日期=(today-timedelta(days=1)).isoformat()),
            row('closed', 截止日期=today.isoformat(), 状态='已完成'),
            row('bad', 截止日期='bad'),
            row('risk', 'risks', 等级='高'), row('low', 'risks', 等级='低'),
            row('pending', 'decisions'), row('decided', 'decisions', 结论='同意'),
            row('soon', 'contracts', 到期日期=(today+timedelta(days=30)).isoformat()),
            row('expired', 'contracts', 到期日期=(today-timedelta(days=1)).isoformat()),
            row('later', 'contracts', 到期日期=(today+timedelta(days=31)).isoformat()),
        ]
        for module, kind, expected in [('tasks', 'today', 'today'), ('tasks', 'overdue', 'old'), ('risks', 'high_risk', 'risk'), ('decisions', 'pending_decision', 'pending'), ('contracts', 'expiring', 'soon')]:
            self.window.open_workspace_filter(module, kind)
            table = self.window.page.findChild(QTableWidget)
            self.assertEqual(table.rowCount(), 1)
            self.assertEqual(table.item(0, 0).data(Qt.UserRole)['id'], expected)
            self.window.refresh()
            self.assertEqual(self.window.page.findChild(QTableWidget).rowCount(), 1)
            self.button('清空筛选').click()
            self.assertGreater(self.window.page.findChild(QTableWidget).rowCount(), 1)

    def test_detail_escapes_markup_and_blocks_non_web_links(self):
        self.window.records = [row('safe', 名称='<script>alert(1)</script>', 证据='<img src="file:///secret"> https://example.com/a?x=1&y=2')]
        table = self.page()
        table.selectRow(0)
        browser = self.window.page.findChild(QTextBrowser, 'workspace_details')
        self.assertFalse(browser.openLinks())
        self.assertFalse(browser.openExternalLinks())
        self.assertIn('<script>', browser.toPlainText())
        markup = self.window._detail_html(self.window.records[0])
        self.assertNotIn('<script>', markup)
        self.assertNotIn('<img', markup)
        self.assertIn('href="https://example.com', markup)
        with patch('itmanager.workspace_ui.QMessageBox.question') as ask, patch('itmanager.workspace_ui.QDesktopServices.openUrl') as open_url:
            for url in ('file:///secret', 'javascript:alert(1)', 'data:text/plain,hello'):
                self.window._open_detail_link(QUrl(url))
            ask.assert_not_called()
            open_url.assert_not_called()
            from PySide6.QtWidgets import QMessageBox
            ask.return_value = QMessageBox.No
            self.window._open_detail_link(QUrl('https://example.com'))
            open_url.assert_not_called()
            ask.return_value = QMessageBox.Yes
            self.window._open_detail_link(QUrl('https://example.com'))
            open_url.assert_called_once()

    def test_calendars_owner_resolution_and_fixed_width(self):
        self.window.records = [row('person', 'people', 姓名='小林'), row('log', 'logs', 人员ID='person', 日期='2025-01-01', 工作内容='长' * 10000)]
        table = self.page('logs')
        self.assertEqual(self.window.record_owner(self.window.records[1]), '小林')
        self.assertEqual(self.window.record_date(self.window.records[1]), '2025-01-01')
        self.assertTrue(all(c.calendarPopup() for c in self.window.page.findChildren(QDateEdit)))
        self.assertLessEqual(max(table.columnWidth(i) for i in range(table.columnCount())), 420)

    def test_dashboard_real_lists(self):
        self.window.records = [row('today', 截止日期=date.today().isoformat()), row('old', 截止日期=(date.today()-timedelta(days=1)).isoformat())]
        self.window.refresh()
        tables = self.window.page.findChildren(QTableWidget)
        self.assertEqual(len(tables), 2)
        self.assertEqual(sorted(t.item(0, 0).text() for t in tables), ['old', 'today'])


if __name__ == '__main__':
    unittest.main()
