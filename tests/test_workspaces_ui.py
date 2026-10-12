"""Offline Qt tests: fake clients/runners only; no server or model calls."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from copy import deepcopy
from unittest.mock import patch
from uuid import UUID

try:
    from PySide6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QDialog, QDialogButtonBox
    from itmanager.forms import FormDialog
    from itmanager.workspaces_ui import WorkspaceSystemsMixin, TemplateEditor, entry_fields, _fill
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


CONFIG = {'name': 'IPO IT准备与证据梳理', 'description': '不替代专业审计',
          'phases': [{'key': 'p1', 'name': '准备', 'deliverables': ['证据'], 'checklist': ['复核'], 'extra': 2}],
          'work_packages': [{'key': 'w1', 'name': '盘点', 'phase': 'p1', 'description': '说明', 'extra': 3}],
          'forms': [{'key': 'survey', 'name': '调研', 'fields': [
              {'key': 'title', 'label': '标题', 'type': 'text', 'required': True},
              {'key': 'note', 'label': '说明', 'type': 'long'},
              {'key': 'day', 'label': '日期', 'type': 'date'},
              {'key': 'result', 'label': '结论', 'type': 'choice', 'options': ['通过', '整改']}]}],
          'prompts': [{'name': '专项分析', 'template': '{{content}}'}], 'report_sections': ['概况']}


class Client:
    def __init__(self): self.calls = []
    def get(self, path):
        self.calls.append(('GET', path))
        if '/ai-preview?' in path:return {'title':'专项','context':'材料','template':'{{content}}','context_hash':'hash'}
        if path == '/workspace-templates': return [{'id': 'ipo', 'version': 1, 'config': deepcopy(CONFIG)}]
        if path == '/workspaces': return [{'id': 'ws1', 'name': '专项', 'owner': '张', 'template_snapshot': deepcopy(CONFIG)}]
        return []
    def post(self, path, payload):
        self.calls.append(('POST', path, deepcopy(payload)))
        return {'id': 'saved'}


class Runner:
    def __init__(self): self.pending = []
    def submit(self, work, done, failed): self.pending.append((work, done, failed))
    def finish(self):
        work, done, failed = self.pending.pop(0)
        try: value = work()
        except Exception as exc: failed(str(exc))
        else: done(value)


if QT_AVAILABLE:
    class Harness(WorkspaceSystemsMixin, QMainWindow):
        def __init__(self):
            super().__init__()
            self.client = Client(); self.runner = Runner(); self.generation = 1
            self.current = 'workspaces'; self.records = []; self.config = {'providers': []}
            root = QWidget(); self.setCentralWidget(root); self.page_layout = QVBoxLayout(root)
            self.errors = []; self.drafts = []
            self.workspaces_page()
        def error(self, message): self.errors.append(message)
        def draft_editor(self, draft): self.drafts.append(draft)


@unittest.skipUnless(QT_AVAILABLE, 'PySide6 not installed')
class WorkspacesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])
    def setUp(self): self.ui = Harness()
    def tearDown(self): self.ui.close(); self.ui.deleteLater(); self.app.processEvents()

    def test_two_tabs_load_and_clear_names(self):
        self.ui.runner.finish(); self.ui.runner.finish()
        self.assertEqual(self.ui.ws_table.item(0, 0).text(), '专项')
        self.assertEqual(self.ui.ws_template_table.item(0, 0).text(), CONFIG['name'])
        self.assertTrue(self.ui.ws_create_button.isEnabled())

    def test_captured_client_and_generation_discard_stale_result(self):
        old = self.ui.client
        self.ui.client = Client(); self.ui.generation += 1
        self.ui.runner.finish(); self.ui.runner.finish()
        self.assertEqual(len(old.calls), 2)
        self.assertEqual(self.ui.client.calls, [])
        self.assertEqual(self.ui.ws_table.rowCount(), 0)

    def test_stale_errors_ignored(self):
        _, _, failed = self.ui.runner.pending[0]
        self.ui.current = 'dashboard'; failed('late error')
        self.assertEqual(self.ui.errors, [])

    def test_request_id_stable_for_same_data_after_failure(self):
        data = {'template_id': 'ipo', 'name': '专项', 'owner': '张', 'start_date': '2026-01-01', 'due_date': '2026-12-31'}
        first = self.ui._ws_creation_payload(data)
        UUID(first['request_id'])
        self.assertEqual(first, self.ui._ws_creation_payload(deepcopy(data)))
        changed = self.ui._ws_creation_payload(dict(data, owner='李'))
        self.assertNotEqual(first['request_id'], changed['request_id'])

    def test_schema_form_required_date_choices(self):
        fields, choices = entry_fields(CONFIG['forms'][0])
        dialog = FormDialog('测试', fields, choices=choices)
        self.assertFalse(dialog.validate())
        dialog.widgets['title'][0].setText('证据')
        dialog.widgets['day'][0].setText('not-date')
        self.assertFalse(dialog.validate())
        dialog.widgets['day'][0].setText('2026-01-02')
        self.assertTrue(dialog.validate())
        self.assertEqual(dialog.widgets['result'][0].itemData(1), '通过')
        dialog.deleteLater()

    def test_template_structured_edit_preserves_snapshot_and_metadata(self):
        before = deepcopy(CONFIG); dialog = TemplateEditor(CONFIG)
        dialog.name.setText('副本')
        table, _ = dialog.tables['phases']; table.item(0, 1).setText('新阶段')
        value = dialog.values()
        self.assertEqual(value['phases'][0]['extra'], 2)
        self.assertEqual(value['phases'][0]['name'], '新阶段')
        self.assertEqual(value['forms'], CONFIG['forms'])
        self.assertEqual(CONFIG, before)
        dialog.deleteLater()

    def test_all_builtin_templates_roundtrip_backend_validation(self):
        from itmanager.workspaces import builtin_templates, validate_template
        templates = builtin_templates()
        values = templates.values() if isinstance(templates, dict) else templates
        for template in values:
            config = template.get('config', template)
            dialog = TemplateEditor(config)
            self.assertEqual(validate_template(dialog.values()), config)
            dialog.deleteLater()

    def test_entry_cas_and_status_payload(self):
        self.ui.runner.pending.clear()
        entry = {'id': 'e1', 'version': 4, 'form_key': 'survey', 'status': 'draft', 'data': {'title': '证据'}}
        def submit(dialog):
            dialog.widgets['__entry_status'][0].setCurrentText('submitted')
            dialog.buttons.accepted.emit()
            self.ui.runner.finish()
            return QDialog.Accepted
        with patch.object(FormDialog, 'exec', submit):
            self.ui._ws_entry('/workspaces/ws1', CONFIG['forms'][0], entry, lambda: None)
        method, path, payload = self.ui.client.calls[-1]
        self.assertEqual(path, '/workspaces/ws1/entries')
        self.assertEqual(payload['id'], 'e1'); self.assertEqual(payload['version'], 4)
        self.assertEqual(payload['status'], 'submitted')
        self.assertNotIn('__entry_status', payload['data'])
        self.assertEqual(entry['status'], 'draft')

    def test_ai_preview_then_sensitive_confirmation(self):
        from PySide6.QtWidgets import QMessageBox
        self.ui.runner.pending.clear()
        self.ui.config = {'providers': [{'id': 'p1', 'name': '模型'}]}
        parent = QDialog(self.ui)
        def choose(dialog):
            dialog.widgets['provider_id'][0].setCurrentIndex(1)
            dialog.widgets['prompt_name'][0].setCurrentIndex(1)
            return QDialog.Accepted
        with patch.object(FormDialog, 'exec', choose), patch.object(QDialog, 'exec', return_value=QDialog.Accepted), patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
            self.ui._ws_ai('/workspaces/ws1', {'template_snapshot': deepcopy(CONFIG)}, parent)
            self.ui.runner.finish()
            self.ui.runner.finish()
        self.assertIn('/ai-preview?prompt_name=', self.ui.client.calls[0][1])
        payload = self.ui.client.calls[-1][2]
        self.assertEqual(payload, {'provider_id': 'p1', 'prompt_name': '专项分析', 'confirmed': True, 'sensitive_confirmed': True,'context_hash':'hash'})
        self.assertEqual(self.ui.drafts, [{'id': 'saved'}])

    def test_ai_sensitive_refusal_never_posts(self):
        from PySide6.QtWidgets import QMessageBox
        self.ui.runner.pending.clear()
        self.ui.config = {'providers': [{'id': 'p1', 'name': '模型'}]}
        parent = QDialog(self.ui)
        def choose(dialog):
            dialog.widgets['provider_id'][0].setCurrentIndex(1)
            dialog.widgets['prompt_name'][0].setCurrentIndex(1)
            return QDialog.Accepted
        with patch.object(FormDialog, 'exec', choose), patch.object(QDialog, 'exec', return_value=QDialog.Accepted), patch.object(QMessageBox, 'question', return_value=QMessageBox.No):
            self.ui._ws_ai('/workspaces/ws1', {'template_snapshot': deepcopy(CONFIG)}, parent)
            self.ui.runner.finish()
        self.assertEqual(self.ui.runner.pending, [])
        self.assertFalse(any(c[0] == 'POST' for c in self.ui.client.calls))

    def test_import_is_json_data_and_new_template(self):
        import json
        from pathlib import Path
        from PySide6.QtWidgets import QFileDialog
        self.ui.runner.pending.clear()
        malicious_text = '__import__("os").system("no")'
        config = dict(deepcopy(CONFIG), description=malicious_text)
        with patch.object(QFileDialog, 'getOpenFileName', return_value=('template.json', '')), patch.object(Path, 'read_text', return_value=json.dumps(config)), patch.object(TemplateEditor, 'exec', return_value=QDialog.Accepted):
            self.ui._ws_import(); self.ui.runner.finish()
        payload = self.ui.client.calls[-1][2]
        self.assertNotIn('id', payload)
        self.assertEqual(payload['data']['description'], malicious_text)

    def test_dialog_close_discards_callback(self):
        self.ui.runner.pending.clear(); dialog = QDialog(self.ui); seen = []
        self.ui._ws_request('POST', '/workspaces/ws1/report', {}, seen.append, dialog)
        dialog.reject(); self.ui.runner.finish()
        self.assertEqual(seen, [])

    def test_post_payload_copied_before_worker(self):
        self.ui.runner.pending.clear(); payload = {'data': {'title': 'original'}}
        self.ui._ws_request('POST', '/workspaces/ws1/entries', payload, lambda _: None)
        payload['data']['title'] = 'changed'; self.ui.runner.finish()
        self.assertEqual(self.ui.client.calls[-1][2]['data']['title'], 'original')


if __name__ == '__main__': unittest.main()
