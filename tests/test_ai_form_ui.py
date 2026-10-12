import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit
from itmanager.forms import FormDialog, RecordDialog
from itmanager.schema import MODULES
from itmanager.ai_form_ui import apply_fields, attach_ai_assistant


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


class Runner:
    def __init__(self):
        self.jobs = []

    def submit(self, work, done, failed):
        self.jobs.append((work, done, failed))


class Client:
    def __init__(self):
        self.calls = []

    def post(self, path, payload):
        self.calls.append((path, payload))
        return {'fields': {'事项': '建议'}, 'suggestions': ['手动核实'], 'warnings': ['谨慎']}


def open_ui(module='tasks', records=()):
    form = RecordDialog(module)
    runner, client = Runner(), Client()
    button = attach_ai_assistant(form, module, records, [{'id': 'p', 'enabled': True}], [
        {'id': 'wrong', 'purpose': 'report', 'module': module},
        {'id': 'disabled', 'purpose': 'form_fill', 'module': module, 'enabled': False},
        {'id': 'first', 'purpose': 'form_fill', 'module': module},
        {'id': 'default', 'purpose': 'form_fill', 'module': module, 'is_default': True},
    ], runner, client)
    button.click()
    return form, form.ai_assistant, runner, client


def test_apply_preserves_baseline_and_protects_fields(app):
    form = FormDialog('test', [('a', 'a', 'text', ''), ('b', 'b', 'long', ''),
                               ('date', 'date', 'date', ''), ('old', 'old', 'text', 'old'),
                               ('locked', 'locked', 'text', '')])
    baseline = dict(form.baseline)
    form.widgets['locked'][0].setReadOnly(True)
    result = apply_fields(form, {'a': 'A', 'b': 'B', 'date': '2026-06-01', 'old': 'new', 'locked': 'x', 'unknown': 'x'},
                          ['a', 'b', 'date', 'old', 'locked', 'unknown'])
    assert result['applied'] == ['a', 'b', 'date']
    assert set(result['skipped']) == {'old', 'locked', 'unknown'}
    assert form.baseline == baseline
    assert form.is_dirty()
    assert apply_fields(form, {'old': 'new'}, ['old'], overwrite=True)['applied'] == ['old']
    form.widgets['a'][0].setEnabled(False)
    assert 'a' in apply_fields(form, {'a': 'x'}, ['a'], True)['skipped']


def test_combo_known_unknown_and_editable(app):
    form = FormDialog('test', [('人员ID', '人员ID', 'text', ''), ('choice', 'choice', ['', 'a'], ''), ('free', 'free', [''], '')],
                      choices={'人员ID': [('Known', 'id1')]})
    free = form.widgets['free'][0]
    free.setEditable(True)
    relation = form.widgets['人员ID'][0]
    relation.setEditable(True)
    before = relation.count()
    result = apply_fields(form, {'人员ID': 'invented', 'choice': 'invented', 'free': 'new'}, ['人员ID', 'choice', 'free'])
    assert result['applied'] == ['free']
    assert relation.count() == before
    assert apply_fields(form, {'人员ID': 'id1', 'choice': 'a'}, ['人员ID', 'choice'])['applied'] == ['人员ID', 'choice']


def test_all_modules_attach_and_finalized(app):
    for module in MODULES:
        form = RecordDialog(module)
        button = attach_ai_assistant(form, module, [], [], [], Runner(), Client())
        assert form.layout().itemAt(0).widget() is button
        assert attach_ai_assistant(form, module, [], [], [], Runner(), Client()) is button
    final = RecordDialog('reviews', {'data': {'状态': '已定稿'}})
    assert not attach_ai_assistant(final, 'reviews', [], [], [], Runner(), Client()).isEnabled()
    assert not apply_fields(final, {'状态': '待复核'}, ['状态'], True)['applied']


def test_async_payload_busy_failure_preserves_input(app, monkeypatch):
    records = [{'id': 's', 'module': 'people', 'data': {'姓名': 'Person'}}]
    form, ui, runner, client = open_ui(records=records)
    assert form.busy
    assert ui.prompt.count() == 2
    assert ui.prompt.currentData()['id'] == 'default'
    assert ui.selected_context() == []
    ui.context.item(0, 0).setCheckState(Qt.Checked)
    ui.basis.setPlainText('my basis')
    previews, confirmations = [], []
    monkeypatch.setattr(ui, 'preview', lambda payload, rows: previews.append((payload.copy(), rows)) or True)
    monkeypatch.setattr('itmanager.ai_form_ui.ask', lambda parent, text: confirmations.append(text) or True)
    ui.submit()
    ui.submit()
    assert len(runner.jobs) == 1
    assert ui.busy and not ui.basis.isEnabled()
    ui.reject()
    assert ui.isVisible()
    form.close()
    assert form.busy
    work, done, failed = runner.jobs[0]
    work()
    path, payload = client.calls[0]
    assert path == '/ai/form-fill'
    assert payload['basis'] == 'my basis'
    assert payload['context_ids'] == ['s']
    assert payload['confirmed'] and payload['sensitive_confirmed']
    assert payload['current'] == form.values()
    assert len(confirmations) == 1 and previews[0][1] == records
    failed('offline')
    assert not ui.busy and ui.basis.toPlainText() == 'my basis'
    assert 'offline' in ui.error.text()
    ui.reject()
    assert not form.busy


def test_context_limit_result_selection_and_overwrite(app, monkeypatch):
    records = [{'id': str(i), 'module': 'tasks', 'data': {}} for i in range(31)]
    form, ui, _, _ = open_ui(records=records)
    for i in range(31):
        ui.context.item(i, 0).setCheckState(Qt.Checked)
    assert len(ui.selected_context()) == 30
    keys = [k for k, (w, _) in form.widgets.items() if isinstance(w, QLineEdit)]
    empty, occupied = keys[:2]
    form.widgets[occupied][0].setText('original')
    baseline = dict(form.baseline)
    ui._completed({'fields': {empty: 'suggestion', occupied: 'replacement', 'unknown': 'x'}, 'suggestions': ['manual'], 'warnings': ['warn']})
    assert ui.results.item(0, 0).checkState() == Qt.Checked
    assert ui.results.item(1, 0).checkState() == Qt.Unchecked
    assert ui.results.item(2, 0).checkState() == Qt.Unchecked
    assert 'manual' in ui.warnings.toPlainText()
    ui.results.item(1, 0).setCheckState(Qt.Checked)
    monkeypatch.setattr('itmanager.ai_form_ui.ask', lambda *args: False)
    ui.apply_selected()
    assert form.values()[occupied] == 'original'
    monkeypatch.setattr('itmanager.ai_form_ui.ask', lambda *args: True)
    ui.apply_selected()
    assert form.values()[empty] == 'suggestion'
    assert form.values()[occupied] == 'replacement'
    assert form.baseline == baseline and form.is_dirty()
    ui.reject()


def test_missing_configuration_and_sensitive_current_module(app, monkeypatch):
    form = RecordDialog('tasks')
    attach_ai_assistant(form, 'tasks', [], [], [], Runner(), Client()).click()
    ui = form.ai_assistant
    assert not ui.submit_button.isEnabled()
    assert '系统设置→提示词库' in ui.error.text()
    ui.reject()
    form, ui, runner, _ = open_ui('people')
    monkeypatch.setattr(ui, 'preview', lambda *args: True)
    monkeypatch.setattr('itmanager.ai_form_ui.ask', lambda *args: False)
    ui.submit()
    assert not runner.jobs and not ui.busy
    ui.reject()
