"""Offscreen checks: mocked API and runner; no server/network required."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QApplication, QDialog, QDialogButtonBox, QFileDialog, QMainWindow, QMessageBox,
        QPlainTextEdit, QTableWidgetItem, QVBoxLayout, QWidget,
    )
    from itmanager.checklist_templates import SAFE_TEMPLATE_ERRORS
    from itmanager.checklists_ui import ChecklistTemplateDialog, ChecklistsMixin, TEMPLATES
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


CUSTOM_TEMPLATES = [
    {'id': 'tpl-1', 'name': '自定义检查', 'description': '自定义说明', 'scope': '通用', 'enabled': True, 'version': 3,
     'items': [{'name': '自定义项一', 'hint': '通过判定：有证据', 'required': True},
               {'name': '自定义项二', 'hint': '', 'required': False}]},
    {'id': 'tpl-2', 'name': '备份运行', 'description': '内置备份检查', 'scope': 'recovery', 'enabled': True, 'version': 7,
     'items': [{'name': '备份作业运行结果', 'hint': '通过判定：作业成功', 'required': True}]},
]


class Client:
    def __init__(self):
        self.calls = []
        self.rows = []
        self.templates = None
        self.fail_post = False
        self.fail_get = False

    def get(self, path):
        self.calls.append(('GET', path))
        if self.fail_get:
            raise RuntimeError('离线')
        if path == '/checklist-templates':
            return [] if self.templates is None else self.templates
        return self.rows if path == '/checklists/runs' else self.rows[0]

    def post(self, path, body):
        self.calls.append(('POST', path, body))
        if self.fail_post:
            raise RuntimeError('超时')
        run = dict(body, id='run-1', task_ids=['task-1'], risk_ids=['risk-1'])
        self.rows = [run]
        return {'run': run, 'tasks': [{'id': 'task-1'}], 'risks': [{'id': 'risk-1'}]}


class Runner:
    def __init__(self):
        self.pending = []
        self.defer = False

    def submit(self, work, success, failure):
        if self.defer:
            self.pending.append((work, success, failure))
        else:
            self.finish((work, success, failure))

    @staticmethod
    def finish(job):
        work, success, failure = job
        try:
            value = work()
        except Exception as exc:
            failure(str(exc))
        else:
            success(value)


if QT_AVAILABLE:
    class Harness(ChecklistsMixin, QMainWindow):
        def __init__(self):
            super().__init__()
            self.client, self.runner = Client(), Runner()
            self.current, self.generation = 'checklists', 1
            root = QWidget()
            self.setCentralWidget(root)
            self.page_layout = QVBoxLayout(root)
            self.checklists_page()

        def reopen(self):
            self.generation += 1
            while self.page_layout.count():
                item = self.page_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self.checklists_page()


@unittest.skipUnless(QT_AVAILABLE, 'PySide6 not installed')
class ChecklistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.w = Harness()
        self.confirm = patch('itmanager.checklists_ui.QMessageBox.question', return_value=QMessageBox.Yes)
        self.question = self.confirm.start()

    def tearDown(self):
        self.confirm.stop()
        self.w.close()
        self.w.deleteLater()
        self.app.processEvents()

    def ready(self):
        self.w.checklist_owner.setText('张三')
        for row in range(self.w.checklist_items.rowCount()):
            self.w.checklist_items.cellWidget(row, 1).setCurrentText('通过')

    def template_rows(self):
        """Row values of the template management table, for readable assertions."""
        table = self.w.checklist_template_table
        return [[table.item(row, column).text() for column in range(table.columnCount())]
                for row in range(table.rowCount())]

    def edit_template(self, row_index=0, copy=False):
        """Bind a template editor for one row without opening a modal window."""
        self.w.checklist_template_table.setCurrentCell(row_index, 0)
        with patch.object(ChecklistTemplateDialog, 'exec', lambda dialog: QDialog.Rejected):
            self.w._checklist_template_edit(copy=copy)
        return self.w._checklist_template_dialogs[-1]

    def test_defaults_templates_calendar_validation(self):
        w = self.w
        self.assertEqual(w.checklist_template.count(), 4)
        self.assertTrue(w.checklist_date.calendarPopup())
        for name in TEMPLATES:
            w.checklist_template.setCurrentText(name)
            self.assertTrue(all(i['result'] == '待检查' for i in w.checklist_draft['items']))
            self.assertFalse(w.checklist_submit.isEnabled())
        self.ready()
        w.checklist_items.cellWidget(0, 1).setCurrentText('异常')
        self.assertFalse(w.checklist_submit.isEnabled())
        w._checklist_submit()
        self.assertFalse(any(c[0] == 'POST' for c in w.client.calls))
        w.checklist_items.cellWidget(0, 2).setText('备份失败，需重跑')
        self.assertTrue(w.checklist_submit.isEnabled())
        w._checklist_submit()
        self.assertIn('1 条风险与 1 条整改任务', self.question.call_args.args[2])
        post = next(c for c in w.client.calls if c[0] == 'POST')
        self.assertEqual(post[1], '/checklists/complete')
        UUID(post[2]['request_id'])
        self.assertEqual(set(post[2]), {'name', 'date', 'owner', 'items', 'request_id'})
        self.assertIn('task-1', w.checklist_message.text())
        self.assertIn('risk-1', w.checklist_message.text())
        self.assertEqual(w.checklist_history.rowCount(), 1)
        self.assertFalse(w.checklist_editor.isEnabled())
        self.assertFalse(w.checklist_submit.isEnabled())

    def test_retry_lookup_same_uuid_and_change_new_uuid(self):
        w = self.w
        self.ready()
        w.client.fail_post = True
        w._checklist_submit()
        first = w.client.calls[-1][2]['request_id']
        w._checklist_submit()
        self.assertEqual(w.client.calls[-2], ('GET', '/checklists/runs'))
        self.assertEqual(w.client.calls[-1][2]['request_id'], first)
        w.checklist_owner.setText('李四')
        w._checklist_submit()
        self.assertNotEqual(w.client.calls[-1][2]['request_id'], first)

    def test_uncertain_reconciliation_prevents_duplicate_and_get_failure_blocks(self):
        w = self.w
        self.ready()
        w.client.fail_post = True
        w._checklist_submit()
        payload = w.client.calls[-1][2]
        w.client.fail_get = True
        before = sum(c[0] == 'POST' for c in w.client.calls)
        w._checklist_submit()
        self.assertEqual(sum(c[0] == 'POST' for c in w.client.calls), before)
        w.client.fail_get = False
        w.client.rows = [dict(payload, id='existing', task_ids=['task-old'])]
        w._checklist_submit()
        self.assertEqual(sum(c[0] == 'POST' for c in w.client.calls), before)
        self.assertIn('task-old', w.checklist_message.text())

    def test_cancel_no_post_and_cache_across_navigation(self):
        self.ready()
        self.w.checklist_items.cellWidget(0, 2).setText('证据已核实')
        self.question.return_value = QMessageBox.No
        self.w._checklist_submit()
        self.assertFalse(any(c[0] == 'POST' for c in self.w.client.calls))
        self.w.reopen()
        self.assertEqual(self.w.checklist_owner.text(), '张三')
        self.assertEqual(self.w.checklist_items.cellWidget(0, 2).text(), '证据已核实')
        self.assertEqual(self.w.checklist_items.cellWidget(0, 1).currentText(), '通过')

    def test_generation_ignores_old_history_and_submission_callbacks(self):
        w = self.w
        w.runner.defer = True
        w._checklist_load_runs()
        old_load = w.runner.pending.pop()
        self.ready()
        w._checklist_submit()
        old_submit = w.runner.pending.pop()
        w.current = 'tasks'
        w.generation += 1
        old_load[1]([{'id': 'stale', 'name': '旧页'}])
        old_submit[1]({'run': {'id': 'old'}, 'tasks': [{'id': 'old-task'}]})
        self.assertEqual(w.checklist_history.rowCount(), 0)
        self.assertNotIn('old-task', w.checklist_message.text())
        self.assertTrue(w._checklist_attempt['completed'])

    def test_response_record_ids_and_completed_state_survive_navigation(self):
        w = self.w
        self.ready()
        w.runner.defer = True
        w._checklist_submit()
        job = w.runner.pending.pop()
        w.current = 'tasks'
        w.generation += 1
        job[1]({'run': {'id': 'run-new'}, 'tasks': [{'id': 'full-task'}], 'risks': [{'id': 'full-risk'}]})
        w.current = 'checklists'
        w.runner.defer = False
        w.reopen()
        self.assertIn('full-task', w.checklist_message.text())
        self.assertIn('full-risk', w.checklist_message.text())
        self.assertFalse(w.checklist_submit.isEnabled())
        self.assertFalse(w.checklist_editor.isEnabled())

    def test_retry_uuid_survives_navigation_and_new_resets_results(self):
        w = self.w
        self.ready()
        w.client.fail_post = True
        w._checklist_submit()
        original = w._checklist_attempt['payload']['request_id']
        w.reopen()
        w._checklist_submit()
        self.assertEqual(w._checklist_attempt['payload']['request_id'], original)
        w._checklist_new()
        self.assertIsNone(w._checklist_attempt)
        self.assertTrue(all(i['result'] == '待检查' for i in w.checklist_draft['items']))
        self.assertFalse(w.checklist_submit.isEnabled())

    def test_readonly_detail_shows_problems_and_ids(self):
        w = self.w
        w.client.rows = [{'id': 'run-1', 'items': [{'name': '恢复', 'result': '异常', 'note': '不可恢复', 'task_id': 'task-1', 'risk_id': 'risk-1'}]}]
        w._checklist_load_runs()
        w.checklist_history.selectRow(0)
        captured = []
        def inspect(dialog):
            text = dialog.findChild(QPlainTextEdit)
            captured.append(text.toPlainText())
            self.assertTrue(text.isReadOnly())
            return 0
        with patch.object(QDialog, 'exec', inspect):
            w._checklist_detail()
        self.assertIn('不可恢复', captured[0])
        self.assertIn('risk-1', captured[0])
        self.assertEqual(w.client.calls[-1], ('GET', '/checklists/runs/run-1'))

    # ------------------------------------------------------- configurable templates

    def test_templates_load_from_api_and_fill_dropdown(self):
        w = self.w
        w.client.templates = list(CUSTOM_TEMPLATES)
        w._checklist_load_templates()
        self.assertIn(('GET', '/checklist-templates'), w.client.calls)
        self.assertEqual([w.checklist_template.itemText(i) for i in range(w.checklist_template.count())],
                         ['自定义检查', '备份运行'])
        self.assertEqual([item['name'] for item in w.checklist_draft['items']], ['自定义项一', '自定义项二'])
        self.assertEqual(w.checklist_items.rowCount(), 2)
        self.assertEqual(w.checklist_template_table.rowCount(), 2)
        self.assertEqual(self.template_rows()[1][3:5], ['启用', '7'])

    def test_empty_template_list_falls_back_to_builtin(self):
        w = self.w
        w.client.templates = []
        w._checklist_load_templates()
        self.assertEqual([w.checklist_template.itemText(i) for i in range(w.checklist_template.count())],
                         list(TEMPLATES))
        self.assertEqual(w.checklist_items.rowCount(), len(TEMPLATES['备份运行']))
        self.assertNotIn(('POST', '/checklist-templates'), [call[:2] for call in w.client.calls])

    def test_template_edit_validation_failure_keeps_input(self):
        w = self.w
        w.client.templates = list(CUSTOM_TEMPLATES)
        w._checklist_load_templates()
        dialog = self.edit_template()
        self.assertEqual(dialog.name.text(), '自定义检查')
        posts = sum(call[0] == 'POST' for call in w.client.calls)
        dialog.name.setText('')
        dialog.description.setPlainText('尚未保存的说明')
        dialog.buttons.accepted.emit()
        self.assertEqual(dialog.validation_error, SAFE_TEMPLATE_ERRORS[0])
        self.assertFalse(dialog.busy)
        self.assertEqual(dialog.description.toPlainText(), '尚未保存的说明')
        self.assertEqual(sum(call[0] == 'POST' for call in w.client.calls), posts)
        dialog.name.setText('自定义检查')
        hint = dialog.items.item(0, 1)
        hint.setText('提' * 1001)
        dialog.buttons.accepted.emit()
        self.assertEqual(dialog.validation_error, SAFE_TEMPLATE_ERRORS[6])
        self.assertEqual(hint.text(), '提' * 1001)
        self.assertEqual(sum(call[0] == 'POST' for call in w.client.calls), posts)
        dialog.close()

    def test_import_preview_cancel_saves_nothing(self):
        w = self.w
        payload = {'name': '导入模板', 'description': '导入说明', 'scope': '通用', 'enabled': True,
                   'items': [{'name': '导入项', 'hint': '通过判定：有证据', 'required': True}]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'template.json'
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            seen = []

            def inspect(dialog):
                seen.append(dialog)
                self.assertEqual(dialog.name.text(), '导入模板')
                self.assertEqual(dialog.windowTitle(), '导入预览：核对后保存为新模板（不执行代码）')
                return QDialog.Rejected

            with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(path), 'JSON (*.json)')):
                with patch.object(ChecklistTemplateDialog, 'exec', inspect):
                    w._checklist_template_import()
        self.assertEqual(len(seen), 1)
        self.assertFalse(any(call[0] == 'POST' for call in w.client.calls))

    def test_template_save_carries_version_and_id(self):
        w = self.w
        w.client.templates = list(CUSTOM_TEMPLATES)
        w._checklist_load_templates()
        w.runner.defer = True
        dialog = self.edit_template(row_index=1)  # 备份运行 carries id tpl-2 / version 7
        dialog.name.setText('备份运行（核对版）')
        dialog.name.setText('备份运行')
        dialog.buttons.accepted.emit()
        self.assertTrue(dialog.busy)
        work, done, _ = w.runner.pending.pop()
        work()
        post = w.client.calls[-1]
        self.assertEqual(post[1], '/checklist-templates')
        self.assertEqual(post[2]['id'], 'tpl-2')
        self.assertEqual(post[2]['version'], 7)
        self.assertEqual(post[2]['data']['name'], '备份运行')
        self.assertEqual(set(post[2]['data']), {'name', 'description', 'scope', 'enabled', 'items'})
        self.assertEqual(post[2]['data']['items'][0]['name'], '备份作业运行结果')
        done(w.client.rows[-1])
        self.assertFalse(dialog.busy)

    def test_new_template_payload_has_no_id_or_version(self):
        w = self.w
        w.client.templates = list(CUSTOM_TEMPLATES)
        w._checklist_load_templates()
        w.runner.defer = True
        with patch.object(ChecklistTemplateDialog, 'exec', lambda dialog: QDialog.Rejected):
            dialog = w._checklist_template_edit(new=True)
        dialog.name.setText('全新模板')
        dialog.buttons.accepted.emit()
        work, _, _ = w.runner.pending.pop()
        work()
        post = w.client.calls[-1]
        self.assertNotIn('id', post[2])
        self.assertNotIn('version', post[2])
        self.assertEqual(post[2]['data']['name'], '全新模板')
        self.assertTrue(post[2]['data']['items'])

    def test_save_freezes_form_until_response(self):
        w = self.w
        w.client.templates = list(CUSTOM_TEMPLATES)
        w._checklist_load_templates()
        w.runner.defer = True
        dialog = self.edit_template()
        save = dialog.buttons.button(QDialogButtonBox.Save)
        self.assertEqual(save.text(), '保存')
        dialog.buttons.accepted.emit()
        self.assertTrue(dialog.busy)
        self.assertFalse(dialog.buttons.isEnabled())
        self.assertFalse(dialog.name.isEnabled())
        self.assertFalse(dialog.items.isEnabled())
        self.assertFalse(dialog.scope.isEnabled())
        self.assertFalse(w.checklist_template_panel.isEnabled())
        self.assertFalse(w.checklist_template_table.isEnabled())
        self.assertEqual(save.text(), '保存中…')
        work, done, _ = w.runner.pending.pop()
        done(work())
        self.assertFalse(dialog.busy)
        self.assertTrue(dialog.buttons.isEnabled())
        self.assertTrue(dialog.name.isEnabled())
        self.assertEqual(save.text(), '保存')
        self.assertEqual(w._checklist_template_dialogs[-1], dialog)

    def test_page_rebuild_does_not_write_through_destroyed_widgets(self):
        w = self.w
        w.client.templates = list(CUSTOM_TEMPLATES)
        w._checklist_load_templates()
        w.runner.defer = True
        dialog = self.edit_template()
        posts = sum(call[0] == 'POST' for call in w.client.calls)
        w.reopen()
        self.app.processEvents()
        dialog.buttons.accepted.emit()
        self.assertEqual(sum(call[0] == 'POST' for call in w.client.calls), posts)
        w.runner.defer = False
        self.assertEqual(w.checklist_template_table.rowCount(), 2)
        self.assertEqual([w.checklist_template.itemText(i) for i in range(w.checklist_template.count())],
                         ['自定义检查', '备份运行'])

    def test_stale_template_callback_is_ignored_after_page_switch(self):
        w = self.w
        w.runner.defer = True
        w._checklist_load_templates()
        job = w.runner.pending.pop()
        w.current = 'tasks'
        w.generation += 1
        job[1](list(CUSTOM_TEMPLATES))
        self.assertEqual(w.checklist_template.count(), 4)
        self.assertEqual([w.checklist_template.itemText(i) for i in range(w.checklist_template.count())],
                         list(TEMPLATES))
        job[2]('超时')
        self.assertEqual(w.checklist_template.count(), 4)


if __name__ == '__main__':
    unittest.main()
