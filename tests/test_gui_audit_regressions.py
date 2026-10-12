"""Collected regressions; shared audit cases use only fake/offscreen transport."""
from audit_gui_repro import AuditGuiRepro
from unittest.mock import patch
import unittest
from PySide6.QtWidgets import QDialog
from itmanager.workspaces_ui import TemplateEditor, _fill

class TemplateVersionRegression(unittest.TestCase):
    setUpClass = classmethod(AuditGuiRepro.setUpClass.__func__)
    setUp = AuditGuiRepro.setUp
    tearDown = AuditGuiRepro.tearDown
    ws = AuditGuiRepro.ws
    def test_edit_template_carries_observed_version(self):
        self.ws()
        row = {'id':'t-1', 'version':7, 'config': {'name':'Template','phases':[], 'work_packages':[]}}
        _fill(self.window.ws_template_table, [row], ['id','id','version','id'])
        self.window.ws_template_table.setCurrentCell(0, 0)
        with patch.object(TemplateEditor, 'exec', return_value=QDialog.Accepted):
            self.window._ws_template_edit()
        work, _, _ = self.window.runner.pending.pop()
        work()
        self.assertEqual(self.client.posts[-1][1]['version'], 7)
        self.assertEqual(self.client.posts[-1][1]['id'], 't-1')
        with patch.object(TemplateEditor, 'exec', return_value=QDialog.Accepted):
            self.window._ws_template_edit(copy=True)
        work, _, _ = self.window.runner.pending.pop()
        work()
        self.assertNotIn('version', self.client.posts[-1][1])
        self.assertNotIn('id', self.client.posts[-1][1])
