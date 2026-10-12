"""Read-only desktop audit. Fake transport; no network/config writes.
Run: python -m unittest discover -s tests -p audit_gui_repro.py -v
Regression tests for the defects observed before the explicitly delegated fixes.
"""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QMessageBox
from itmanager.gui import MainWindow
from itmanager.forms import FormDialog

class Client:
    url = 'http://127.0.0.1:18765'
    def __init__(self): self.posts = []
    def post(self, path, payload):
        self.posts.append((path, payload)); return {'id': 'saved'}
    def get(self, path): return []

class DeferredRunner:
    def __init__(self): self.pending = []; self.jobs = {}
    def submit(self, work, done, failed): self.pending.append((work, done, failed))

class AuditGuiRepro(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])
    def setUp(self):
        self.client = Client(); self.window = MainWindow(self.client, autoload=False)
        self.window.runner = DeferredRunner()
        self.window.error = lambda message: setattr(self.window, 'last_error', message)
    def tearDown(self):
        self.window.runner.jobs.clear(); self.window.hide(); self.window.deleteLater()
        self.app.processEvents()
    def ws(self):
        self.window.navigate(self.window.keys.index('workspaces'))
        self.window.runner.pending.clear()
    def editor(self):
        dialog = FormDialog('专项', [('name', '名称 *', 'text', '提交前')], parent=self.window)
        self.window._ws_bind_save(dialog, lambda data: data, '/workspaces/demo/entries', lambda _: None)
        dialog.show(); self.app.processEvents(); return dialog
    def test_ws_saved_old_value_discards_new_input(self):
        self.ws(); dialog = self.editor()
        dialog.buttons.button(QDialogButtonBox.Save).click()
        self.assertTrue(dialog.busy)
        self.assertFalse(dialog.widgets['name'][0].isEnabled())
        dialog.reject()
        self.assertTrue(dialog.isVisible())
        work, done, _ = self.window.runner.pending.pop()
        done(work())
        self.assertEqual(self.client.posts[-1][1]['name'], '提交前')
        self.assertFalse(dialog.isVisible())
        print('PASS E1: editor and cancel frozen during save; result closes editor')
    def test_ws_refresh_invalidates_open_editor_silently(self):
        self.ws(); dialog = self.editor()
        # Exactly the navigate performed by MainWindow.refresh.loaded.
        self.window.navigate(self.window.keys.index('workspaces'))
        self.window.runner.pending.clear()
        dialog.buttons.button(QDialogButtonBox.Save).click()
        self.assertEqual(len(self.window.runner.pending), 1)
        request = self.window.runner.pending.pop()
        request[1](request[0]())
        self.assertFalse(dialog.isVisible())
        print('PASS E2: refresh does not invalidate open editor save')
    def test_ws_response_after_refresh_leaves_save_disabled(self):
        self.ws(); dialog = self.editor()
        dialog.buttons.button(QDialogButtonBox.Save).click()
        request = self.window.runner.pending.pop()
        self.window.navigate(self.window.keys.index('workspaces'))
        request[1](request[0]())
        self.assertFalse(dialog.isVisible())
        self.assertFalse(dialog.busy)
        self.assertEqual(dialog.validation_error, '')
        print('PASS E3: POST result survives page refresh and closes saved editor')
    def test_checklist_return_while_pending_leaves_controls_disabled(self):
        w = self.window
        w.navigate(w.keys.index('checklists')); w.runner.pending.clear()
        w.checklist_owner.setText('审计人员')
        for i in range(w.checklist_items.rowCount()):
            w.checklist_items.cellWidget(i, 1).setCurrentText('通过')
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes): w._checklist_submit()
        request = w.runner.pending.pop()
        w.navigate(w.keys.index('dashboard')); w.navigate(w.keys.index('checklists'))
        self.assertFalse(w.checklist_new.isEnabled())
        request[1]({'id':'run-1','task_ids':[],'risk_ids':[]})
        self.assertFalse(w._checklist_attempt['busy'])
        self.assertTrue(w._checklist_attempt['completed'])
        self.assertTrue(w.checklist_new.isEnabled())
        self.assertIn('run-1', w.checklist_message.text())
        print('PASS E4: returned checklist page unlocks and shows completion')
    def test_checklist_failure_after_return_unlocks_retry(self):
        w = self.window
        w.navigate(w.keys.index('checklists')); w.runner.pending.clear()
        w.checklist_owner.setText('审计人员')
        for i in range(w.checklist_items.rowCount()):
            w.checklist_items.cellWidget(i, 1).setCurrentText('通过')
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes): w._checklist_submit()
        request = w.runner.pending.pop()
        w.navigate(w.keys.index('dashboard')); w.navigate(w.keys.index('checklists'))
        request[2]('timeout')
        self.assertTrue(w.checklist_new.isEnabled())
        self.assertTrue(w.checklist_submit.isEnabled())
        self.assertIn('timeout', w.checklist_message.text())
    def test_ws_error_after_refresh_preserves_input_and_unlocks(self):
        self.ws(); dialog = self.editor()
        dialog.buttons.button(QDialogButtonBox.Save).click()
        request = self.window.runner.pending.pop()
        self.window.navigate(self.window.keys.index('workspaces'))
        request[2]('timeout')
        self.assertFalse(dialog.busy)
        self.assertTrue(dialog.buttons.button(QDialogButtonBox.Save).isEnabled())
        self.assertEqual(dialog.widgets['name'][0].text(), '提交前')
        self.assertEqual(dialog.validation_error, 'timeout')
        dialog.hide()
    def test_chrome_busy_close_and_maximize_are_guarded(self):
        w = self.window; w.show(); self.app.processEvents()
        w.runner.jobs['pending'] = True
        w.title_bar.close_button.click(); self.app.processEvents()
        self.assertTrue(w.isVisible()); self.assertIn('请求处理中', w.last_error)
        w.title_bar.maximize.click(); self.app.processEvents(); self.assertTrue(w.isMaximized())
        w.title_bar.maximize.click(); self.app.processEvents(); self.assertFalse(w.isMaximized())
        w.runner.jobs.clear(); w.title_bar.close_button.click(); self.app.processEvents()
        self.assertFalse(w.isVisible())
        print('PASS: custom chrome busy close blocked, idle close works, maximize/restore state works')
    def test_management_required_and_invalid_date_rejected(self):
        d = FormDialog('延期', [('due_date','新截止日期 *','date',''),('reason','原因 *','long',''),('approver','批准人 *','text','')])
        self.assertFalse(d.validate())
        d.widgets['reason'][0].setPlainText('确需延期'); d.widgets['approver'][0].setText('批准人')
        d.widgets['due_date'][0].setText('2026-02-30'); self.assertFalse(d.validate())
        d.widgets['due_date'][0].setText('2026-03-01'); self.assertTrue(d.validate())
        d.hide()
        print('PASS: task-action required fields and real calendar date validation')

if __name__ == '__main__': unittest.main(verbosity=2)
