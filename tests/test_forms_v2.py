"""Offline tests for the independent v2 forms; no server or external calls."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from unittest.mock import patch

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication, QLineEdit, QComboBox, QPlainTextEdit, QDialogButtonBox
from itmanager.forms import FormDialog, RecordDialog, NullableDateEdit
from itmanager.schema import MODULES, STATUS
from itmanager.gui import CONFIG_FIELDS


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialogs(app):
    created = []
    def make(cls, *args, **kwargs):
        dialog = cls(*args, **kwargs)
        created.append(dialog)
        return dialog
    yield make
    for dialog in created:
        dialog.deleteLater()
    app.processEvents()


def test_all_schema_forms_keep_interface(dialogs):
    for module, (_, names) in MODULES.items():
        dialog = dialogs(RecordDialog, module)
        assert set(dialog.widgets) == set(names)
        assert dialog.tabs.count() == 2
        assert dialog.tabs.tabText(0) == '基础信息'
        assert dialog.form is dialog.forms[0]
        assert dialog.baseline == dialog.values()
        assert not dialog.is_dirty()
        assert dialog.buttons.button(QDialogButtonBox.Save).text() == '保存'
        for widget, kind in dialog.widgets.values():
            if isinstance(widget, QPlainTextEdit):
                assert widget.maximumHeight() <= 128
        if '状态' in names and module != 'reviews':
            combo = dialog.widgets['状态'][0]
            assert [combo.itemText(i) for i in range(combo.count())] == STATUS


def test_date_nullable_calendar_and_invalid_input_retained(dialogs):
    dialog = dialogs(RecordDialog, 'tasks')
    edit = dialog.widgets['截止日期'][0]
    assert isinstance(edit, NullableDateEdit)
    assert isinstance(edit, QLineEdit)
    assert edit.calendarPopup()
    assert edit.text() == ''
    edit.setText('2026-02-30')
    dialog.widgets['名称'][0].setText('测试任务')
    assert not dialog.validate()
    assert '截止日期' in dialog.validation_error
    assert dialog.values()['截止日期'] == '2026-02-30'
    edit._choose_date(QDate(2026, 2, 28))
    assert dialog.values()['截止日期'] == '2026-02-28'
    edit._clear_date()
    assert dialog.values()['截止日期'] == ''
    assert dialog.validate()


def test_date_order_and_task_completion(dialogs):
    dialog = dialogs(RecordDialog, 'tasks', {'data': {'名称': '任务'}})
    dialog.widgets['开始日期'][0].setText('2026-03-05')
    dialog.widgets['截止日期'][0].setText('2026-03-04')
    assert not dialog.validate()
    assert '截止日期' in dialog.validation_error
    dialog.widgets['截止日期'][0].setText('2026-03-06')
    dialog.widgets['状态'][0].setCurrentText('已完成')
    assert not dialog.validate()
    assert '完成证据' in dialog.validation_error
    assert dialog.tabs.currentIndex() == 1
    dialog.widgets['完成证据'][0].setPlainText('验收文档')
    assert not dialog.validate()
    assert '验收人' in dialog.validation_error
    dialog.widgets['验收人'][0].setText('李四')
    assert dialog.validate()


def test_people_suggestions_and_relations_use_titles(dialogs):
    empty = dialogs(RecordDialog, 'tasks')
    assert isinstance(empty.widgets['负责人'][0], QLineEdit)
    records = [
        {'id': 'PEOPLE-123456789abc', 'module': 'people', 'data': {'姓名': '张三'}},
        {'id': 'PROJECTS-123456789abc', 'module': 'projects', 'data': {'名称': '升级项目'}},
    ]
    dialog = dialogs(RecordDialog, 'tasks', records=records)
    owner = dialog.widgets['负责人'][0]
    assert isinstance(owner, QComboBox) and owner.isEditable()
    owner.setCurrentText('外部顾问')
    assert dialog.values()['负责人'] == '外部顾问'
    owner.setCurrentIndex(1)
    assert dialog.values()['负责人'] == '张三'
    relation = dialog.widgets['项目ID'][0]
    relation.setCurrentIndex(1)
    assert '升级项目' in relation.currentText()
    assert records[1]['id'] not in relation.currentText()
    assert dialog.values()['项目ID'] == records[1]['id']
    review = dialogs(RecordDialog, 'reviews', records=records)
    review.widgets['人员ID'][0].setCurrentIndex(1)
    assert '张三' in review.widgets['人员ID'][0].currentText()
    assert review.values()['人员ID'] == records[0]['id']


def test_unknown_choices_and_original_values_preserved(dialogs):
    data = {'名称': '旧任务', '优先级': '紧急P0', '状态': '历史状态', '项目ID': 'missing-id'}
    dialog = dialogs(RecordDialog, 'tasks', {'data': data})
    for key, value in data.items():
        assert dialog.values()[key] == value
    assert '高' in [dialog.widgets['优先级'][0].itemText(i) for i in range(dialog.widgets['优先级'][0].count())]


def test_final_review_reopens_without_adding_empty_fields(dialogs):
    original = {'名称': '旧考核', '状态': '已定稿'}
    dialog = dialogs(RecordDialog, 'reviews', {'id': 'r', 'data': original})
    assert dialog.values() == original
    assert not dialog.widgets['名称'][0].isEnabled()
    assert not dialog.validate()
    dialog.widgets['状态'][0].setCurrentText('待复核')
    assert dialog.validate()
    assert dialog.values() == {'名称': '旧考核', '状态': '待复核'}
    dialog.set_busy(True)
    dialog.set_busy(False)
    assert not dialog.widgets['名称'][0].isEnabled()


def test_review_and_numeric_validation(dialogs):
    dialog = dialogs(RecordDialog, 'reviews', {'data': {'名称': '季度考核'}})
    dialog.widgets['评分'][0].setText('101')
    assert not dialog.validate() and '评分' in dialog.validation_error
    dialog.widgets['评分'][0].setText('-1')
    assert not dialog.validate() and '评分' in dialog.validation_error
    dialog.widgets['评分'][0].setText('90')
    dialog.widgets['状态'][0].setCurrentText('已定稿')
    assert not dialog.validate() and '人员ID' in dialog.validation_error


def test_channel_fields_visibility_filter_and_input_retention(dialogs):
    dialog = dialogs(FormDialog, '通知渠道', CONFIG_FIELDS['channels'], {'name': '邮件通知', 'type': 'email', 'webhook_env': 'OLD_WEBHOOK', 'secret_env': 'OLD_SECRET', 'to_addrs': ['a@example.com']})
    assert dialog.widgets['webhook_env'][0].isHidden()
    assert not dialog.widgets['host'][0].isHidden()
    assert 'webhook_env' not in dialog.values()
    assert 'secret_env' not in dialog.values()
    assert dialog.values()['to_addrs'] == 'a@example.com'
    dialog.widgets['host'][0].setText('smtp.example.com')
    dialog.widgets['from_addr'][0].setText('sender@example.com')
    assert dialog.validate()
    dialog.widgets['type'][0].setCurrentText('feishu')
    assert dialog.widgets['host'][0].isHidden()
    assert not dialog.widgets['webhook_env'][0].isHidden()
    assert 'host' not in dialog.values()
    assert dialog.values()['webhook_env'] == 'OLD_WEBHOOK'
    dialog.widgets['type'][0].setCurrentText('email')
    assert dialog.values()['host'] == 'smtp.example.com'
    assert dialog.widgets['webhook_env'][0].text() == 'OLD_WEBHOOK'


def test_provider_switch_explicit_defaults_never_silent(dialogs):
    dialog = dialogs(FormDialog, '模型', CONFIG_FIELDS['providers'], {'name': '现有模型', 'model': 'custom-model'})
    dialog.widgets['provider'][0].setCurrentText('qwen')
    assert dialog.values()['model'] == 'custom-model'
    assert 'qwen-plus' in dialog.provider_hint.text()
    assert dialog.widgets['model'][0].placeholderText() == 'qwen-plus'
    dialog.apply_provider_defaults()
    assert dialog.values()['model'] == 'qwen-plus'
    assert dialog.values()['api_key_env'] == 'DASHSCOPE_API_KEY'
    assert dialog.validate()


def test_busy_failure_and_discard_guard_preserve_input(dialogs):
    dialog = dialogs(RecordDialog, 'tasks')
    dialog.widgets['名称'][0].setText('未保存输入')
    dialog.set_busy(True)
    assert not dialog.buttons.isEnabled()
    assert not dialog.tabs.isEnabled()
    dialog.reject()
    assert '正在保存' in dialog.validation_error
    dialog.set_busy(False)
    dialog.show_error('保存失败，请重试')
    assert dialog.values()['名称'] == '未保存输入'
    assert dialog.buttons.isEnabled()
    with patch('itmanager.forms.ask', return_value=False) as confirm:
        dialog.reject()
        confirm.assert_called_once()
    assert dialog.values()['名称'] == '未保存输入'


def test_hidden_channel_changes_still_trigger_discard_guard(dialogs):
    dialog = dialogs(FormDialog, '渠道', CONFIG_FIELDS['channels'])
    dialog.widgets['type'][0].setCurrentText('email')
    dialog.widgets['host'][0].setText('edited.example.com')
    dialog.widgets['type'][0].setCurrentText('wecom')
    assert dialog.values() == dialog.baseline
    assert dialog.is_dirty()


def test_default_save_runs_validation(dialogs):
    dialog = dialogs(RecordDialog, 'tasks')
    dialog.buttons.button(QDialogButtonBox.Save).click()
    assert '名称' in dialog.validation_error
    assert dialog.result() == 0
    dialog.widgets['名称'][0].setText('可保存')
    dialog.buttons.button(QDialogButtonBox.Save).click()
    assert dialog.result() == 1
