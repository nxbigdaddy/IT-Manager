"""提醒中心 / 台账数据体检 的离线 Qt 测试：桩客户端 + 桩 runner，无网络、无数据库。

依赖已交付的 itmanager.reminders / itmanager.health 真实实现（纯函数），
因此这里固定的是真实契约，而不是想象的签名。
"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import json
from copy import deepcopy
from datetime import date, timedelta
from unittest.mock import patch
from uuid import UUID

import pytest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QLabel, QMainWindow, QMessageBox,
    QPlainTextEdit, QVBoxLayout, QWidget,
)

from itmanager import health, insights_ui, reminders
from itmanager.forms import FormDialog
from itmanager.management_ui import ActionCreationDialog
from itmanager.schema import MODULES

TODAY = date.today()


# --------------------------------------------------------------------------------------
# 桩：客户端 / 异步执行器
# --------------------------------------------------------------------------------------
class Client:
    def __init__(self):
        self.url = 'http://127.0.0.1:18765'
        self.calls = []
        self.rules = deepcopy(reminders.default_rules())
        self.fail_get = False
        self.fail_post = False

    def get(self, path):
        self.calls.append(('GET', path))
        if self.fail_get:
            raise RuntimeError('离线：无法读取提醒规则')
        if path == '/reminders':
            return deepcopy(self.rules)
        return []

    def post(self, path, payload):
        self.calls.append(('POST', path, deepcopy(payload)))
        if self.fail_post:
            raise RuntimeError('超时：保存结果不确定')
        if path == '/reminders':
            body = deepcopy(payload)
            data = dict(body.get('data') or {})
            data['id'] = body.get('id') or 'rule-1'
            data['version'] = int(body.get('version') or 0) + 1
            self.rules = [row for row in self.rules if row.get('id') != data['id']] + [data]
            return data
        if path == '/actions/create':
            tasks = [{'id': 'task-%d' % index} for index, _ in enumerate(payload.get('items') or [])]
            return {'tasks': tasks, 'logs': [{'id': 'log-' + task['id']} for task in tasks]}
        return {'ok': True}


class Runner:
    """默认同步执行；defer=True 时挂起，便于验证旧回调与异步失败。"""

    def __init__(self):
        self.pending = []
        self.defer = False

    def submit(self, work, done, failed):
        if self.defer:
            self.pending.append((work, done, failed))
            return
        try:
            value = work()
        except Exception as exc:  # noqa: BLE001 - 桩执行器按宿主行为转成失败回调
            failed(str(exc))
        else:
            done(value)

    def settle(self, index=0):
        work, done, failed = self.pending.pop(index)
        try:
            value = work()
        except Exception as exc:  # noqa: BLE001
            failed(str(exc))
        else:
            done(value)


# --------------------------------------------------------------------------------------
# 测试数据
# --------------------------------------------------------------------------------------
def record(identifier, module, updated=None, **data):
    row = {'id': identifier, 'module': module, 'version': 1, 'archived': False, 'data': dict(data)}
    if updated:
        row['updated'] = updated
    return row


def contract(due_days=30, identifier='c1', **overrides):
    data = {'名称': '云服务合同', '供应商': 'A厂', '负责人': '张工',
            '到期日期': (TODAY + timedelta(days=due_days)).isoformat(), '状态': '进行中', '证据': '合同扫描件'}
    data.update(overrides)
    return record(identifier, 'contracts', **data)


def overdue_task(identifier='t1', **overrides):
    data = {'名称': '逾期任务', '截止日期': (TODAY - timedelta(days=5)).isoformat(),
            '状态': '进行中', '负责人': '李工', '完成证据': '待补'}
    data.update(overrides)
    return record(identifier, 'tasks', **data)


def pending_risk(identifier='r1', **overrides):
    data = {'名称': '高风险项', '等级': '高', '负责人': '王工',
            '复核日期': (TODAY + timedelta(days=45)).isoformat(), '状态': '进行中', '复核证据': '待补'}
    data.update(overrides)
    return record(identifier, 'risk')


def healthy_records():
    """一条即将到期合同 + 一条逾期任务 + 一条高风险记录。"""
    return [contract(), overdue_task(), pending_risk()]


class FakeDialog(QDialog):
    """替代 ActionCreationDialog：记录构造参数，模拟成功/失败/取消，并真的发一次 POST。"""

    instances = []
    mode = 'success'
    mutate = None

    def __init__(self, source_id, suggestions, client, runner, parent=None):
        super().__init__(parent)
        self.source_id = source_id
        self.suggestions = deepcopy(suggestions)
        self.client = client
        self.runner = runner
        self.request_id = None
        self.posted_request_id = None
        FakeDialog.instances.append(self)

    def exec(self):
        # 记下本次实际提交的幂等键（构造后、提交前），再模拟人工改写内容
        self.posted_request_id = self.request_id
        if self.mutate is not None:
            for item in self.suggestions:
                item['owner'] = self.mutate
        self.runner.submit(
            lambda: self.client.post('/actions/create', {'source_id': self.source_id,
                                                         'items': self.suggestions,
                                                         'request_id': self.request_id}),
            lambda value: None, lambda message: None)
        return QDialog.Accepted if FakeDialog.mode == 'success' else QDialog.Rejected

    def reject(self):
        pass


# --------------------------------------------------------------------------------------
# 宿主
# --------------------------------------------------------------------------------------
class Harness(insights_ui.InsightsMixin, QMainWindow):
    def __init__(self, records=()):
        super().__init__()
        self.client = Client()
        self.runner = Runner()
        self.records = list(records)
        self.generation = 0
        self.current = 'dashboard'
        self.keys = ['dashboard', insights_ui.REMINDERS_NAV_KEY, insights_ui.HEALTH_NAV_KEY]
        self.edited = []
        self.refreshes = 0
        root = QWidget()
        self.setCentralWidget(root)
        self.page_layout = QVBoxLayout(root)

    def edit_record(self, module, row=None):
        self.edited.append((module, row))

    def refresh(self):
        self.refreshes += 1

    def clear(self):
        while self.page_layout.count():
            item = self.page_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout() is not None:
                item.layout().deleteLater()

    def open_page(self, key):
        self.clear()
        self.generation += 1
        self.current = key
        if key == insights_ui.REMINDERS_NAV_KEY:
            self.reminders_page()
        else:
            self.health_page()

    @property
    def posts(self):
        return [call for call in self.client.calls if call[0] == 'POST']

    def column(self, table, header):
        for index in range(table.columnCount()):
            if table.horizontalHeaderItem(index).text() == header:
                return [table.item(row, index).text() for row in range(table.rowCount())]
        raise AssertionError('未找到列：' + header)


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------
@pytest.fixture(scope='module')
def qapp():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def no_blocking_dialogs():
    """安全网：任何未被显式 patch 的模态调用都立即返回，测试绝不能真的等待用户输入。

    MainWindow.closeEvent 在还有异步任务时会调用 self.error() → QMessageBox.warning；
    离屏环境下真实模态会永久阻塞，所以这里把所有模态入口都替换成确定值。
    """
    class _NoModal:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def stub(*args, **kwargs):
                self.calls.append((name, args, kwargs))
                return QDialog.Rejected

            return stub

    recorder = _NoModal()
    with patch.object(QDialog, 'exec', recorder.exec), \
         patch.object(QMessageBox, 'question', recorder.question), \
         patch.object(QMessageBox, 'warning', recorder.warning), \
         patch.object(QMessageBox, 'information', recorder.information), \
         patch.object(QMessageBox, 'critical', recorder.critical), \
         patch.object(QFileDialog, 'getSaveFileName', recorder.getSaveFileName), \
         patch.object(QFileDialog, 'getOpenFileName', recorder.getOpenFileName):
        yield recorder


def make_ui(records=(), rules=None):
    ui = Harness(records)
    if rules is not None:
        ui.client.rules = deepcopy(rules)
    return ui


def _open_rules_dialog(ui, title='编辑提醒规则', rule=None):
    rule = rule if rule is not None else ui._reminders_rules[0]
    return insights_ui.RulesDialog(title, rule, ui.client, ui.runner, ui._reminders_guard(),
                                   ui._reminders_saved, ui)


# --------------------------------------------------------------------------------------
# 0 页面构建 / 真实 MainWindow 回归
# --------------------------------------------------------------------------------------
def test_reminders_page_builds_empty_and_with_records(qapp):
    empty = make_ui()
    empty.open_page(insights_ui.REMINDERS_NAV_KEY)
    assert empty.reminders_table.rowCount() == 0
    assert empty._reminders_rules_table.rowCount() == len(reminders.default_rules())
    assert '暂无到期提醒' in empty.reminders_empty.text()
    assert '补齐日期' in empty.reminders_empty.text()
    assert '不发送任何消息' in empty.page_layout.itemAt(0).widget().text()
    assert empty.reminders_table.horizontalHeaderItem(5).text() == '剩余天数'
    assert empty.reminders_table.horizontalHeaderItem(6).text() == '建议动作'

    filled = make_ui(healthy_records())
    filled.open_page(insights_ui.REMINDERS_NAV_KEY)
    assert filled.reminders_table.rowCount() == 2
    assert filled.reminders_empty.text() == ''
    filled.close()
    empty.close()


def test_health_page_builds_empty_and_with_records(qapp):
    empty = make_ui()
    empty.open_page(insights_ui.HEALTH_NAV_KEY)
    assert empty.health_table.rowCount() == 0
    assert '未发现明显问题' in empty.health_empty.text()
    assert '合规结论' in empty.health_empty.text()

    records = healthy_records()
    filled = make_ui(records)
    filled.open_page(insights_ui.HEALTH_NAV_KEY)
    scanned = health.scan(records, TODAY)
    expected = scanned['findings']
    assert filled.health_table.rowCount() == len(expected) > 0
    labels = [label.text() for label in filled._health_stats_widget.findChildren(QLabel)]
    assert f'扫描记录数 {scanned["scanned"]}' in labels
    assert f'发现总计 {len(expected)}' in labels
    assert any('生成时间' in text for text in labels)
    filled.close()
    empty.close()


def test_real_mainwindow_host_opens_both_pages(qapp):
    """父代理接线冒烟：真实 MainWindow 逐个打开两个页面不得抛异常，且行数正确。

    使用同步 runner 桩替代线程池，避免离屏环境下等待后台线程；页面代码与宿主接线完全真实。
    """
    from itmanager.gui import MainWindow

    client = Client()
    window = MainWindow(client, autoload=False)
    window.runner = Runner()  # 同步执行，避免后台线程在离屏环境下等待
    records = healthy_records()
    window.records = deepcopy(records)
    window.keys = list(window.keys) + [insights_ui.REMINDERS_NAV_KEY, insights_ui.HEALTH_NAV_KEY]
    assert callable(window.edit_record) and callable(window.refresh) and window.statusBar() is not None

    window.navigate(window.keys.index(insights_ui.REMINDERS_NAV_KEY))  # 曾经在此崩溃
    reminder_items = reminders.compute_items(records, reminders.default_rules(), TODAY)
    assert window.current == insights_ui.REMINDERS_NAV_KEY
    assert window.reminders_table.rowCount() == len(reminder_items) == 2
    assert [window.reminders_table.item(row, 0).text() for row in range(2)] == ['高', '高']
    assert window._reminders_rules_table.rowCount() == len(reminders.default_rules())
    labels = [label.text() for label in window._reminders_stats_widget.findChildren(QLabel)]
    assert '提醒总计 2' in labels and '高 2' in labels

    window.navigate(window.keys.index(insights_ui.HEALTH_NAV_KEY))
    findings = health.scan(records, TODAY)
    assert window.current == insights_ui.HEALTH_NAV_KEY
    assert window.health_table.rowCount() == len(findings['findings']) > 0
    stats = [label.text() for label in window._health_stats_widget.findChildren(QLabel)]
    assert f'扫描记录数 {findings["scanned"]}' in stats
    window.deleteLater()  # close() 会被 closeEvent/error() 的真实模态拦住，这里直接回收


def test_missing_dependencies_degrade_without_crash(qapp):
    with patch.object(insights_ui, 'reminders_service', None):
        ui = make_ui()
        ui.open_page(insights_ui.REMINDERS_NAV_KEY)
        assert ui.reminders_available is False
        assert '提醒服务不可用' in ui.reminders_service_notice.text()
        assert ui.reminders_table.rowCount() == 0
        assert ui._reminders_rules_table.rowCount() >= 1  # 内置默认规则
        assert '补齐日期' in ui.reminders_empty.text()
    ui.close()

    with patch.object(insights_ui, 'health_service', None):
        degraded = make_ui(healthy_records())
        degraded.open_page(insights_ui.HEALTH_NAV_KEY)
        assert degraded.health_available is False
        assert '体检服务不可用' in degraded.health_service_notice.text()
        assert degraded.health_table.rowCount() == 0
        assert '体检服务不可用' in degraded.health_empty.text()
    degraded.close()


# --------------------------------------------------------------------------------------
# 1 计算、渲染、排序、统计
# --------------------------------------------------------------------------------------
def test_compute_items_render_rows_and_authoritative_sorting(qapp):
    records = healthy_records()
    ui = make_ui(records)
    ui.open_page(insights_ui.REMINDERS_NAV_KEY)
    items = reminders.compute_items(records, reminders.default_rules(), TODAY)
    assert len(items) == 2
    assert [ui.reminders_table.item(row, 0).text() for row in range(2)] == [item['severity'] for item in items]
    assert [ui.reminders_table.item(row, 1).text() for row in range(2)] == [item['rule_name'] for item in items]
    assert [ui.reminders_table.item(row, 3).text() for row in range(2)] == [item['title'] for item in items]
    assert [ui.reminders_table.item(row, 4).text() for row in range(2)] == [item['date'] for item in items]
    # 剩余天数：逾期显示“已逾期N天”，未来显示“剩余N天”
    days = [ui.reminders_table.item(row, 5).text() for row in range(2)]
    assert days[0] == '已逾期5天'
    assert days[1] == '剩余30天'
    assert [ui.reminders_table.item(row, 6).text() for row in range(2)] == [item['message'] for item in items]
    # 统计条来自 reminders.summarize（英文键 → 页面中文标签）
    summary = reminders.summarize(items)
    labels = [label.text() for label in ui._reminders_stats_widget.findChildren(QLabel)]
    assert f'提醒总计 {summary["total"]}' in labels
    assert f'高 {summary["high"]}' in labels and f'中 {summary["medium"]}' in labels and f'低 {summary["low"]}' in labels
    assert labels.count('提醒总计 2') == 1  # 重绘不会叠加卡片
    ui.close()


def test_health_filters_by_severity_and_module(qapp):
    records = healthy_records()
    ui = make_ui(records)
    ui.open_page(insights_ui.HEALTH_NAV_KEY)
    findings = health.scan(records, TODAY)['findings']
    assert [ui.health_table.item(row, 0).text() for row in range(len(findings))] == [
        finding['severity'] for finding in findings]

    ui.health_level_filter.setCurrentIndex(ui.health_level_filter.findData('高'))
    expected_high = [finding for finding in findings if finding['severity'] == '高']
    assert ui.column(ui.health_table, '等级') == ['高'] * len(expected_high)
    assert ui.column(ui.health_table, '字段') == [finding['field'] for finding in expected_high]

    ui.health_level_filter.setCurrentIndex(0)
    ui.health_module_filter.setCurrentIndex(ui.health_module_filter.findData('tasks'))
    expected_tasks = [finding for finding in findings if finding['module'] == 'tasks']
    assert ui.column(ui.health_table, '事项') == [finding['title'] for finding in expected_tasks]
    assert ui.health_empty.text() == ''
    ui.close()


def test_reminder_double_click_opens_record(qapp):
    records = healthy_records()
    ui = make_ui(records)
    ui.open_page(insights_ui.REMINDERS_NAV_KEY)
    ui.reminders_table.selectRow(0)
    ui._reminders_open()
    assert [module for module, _ in ui.edited] == ['tasks']  # 逾期任务排在最前

    ui.records = []  # 记录不在当前已刷新数据中 → 忽略跳转并提示
    ui.reminders_table.selectRow(0)
    ui._reminders_open()
    assert len(ui.edited) == 1
    assert '不在当前已刷新数据中' in ui.reminders_message.text()
    ui.close()


# --------------------------------------------------------------------------------------
# 2 规则管理（10 键权威模型）
# --------------------------------------------------------------------------------------
def test_rule_fields_are_the_ten_authoritative_keys(qapp):
    assert [field[0] for field in insights_ui.rule_fields()] == list(insights_ui.RULE_KEYS)
    rule = reminders.default_rules()[0]
    assert insights_ui.validate_rule(rule) is None
    # 多传一个键就会触发服务端的“包含未知字段”
    with pytest.raises(ValueError) as excinfo:
        insights_ui.validate_rule(dict(rule, kind='expiry'))
    assert str(excinfo.value) in reminders.SAFE_REMINDER_ERRORS


def test_rule_validation_failure_keeps_input_and_skips_post(qapp):
    """真实 RulesDialog（不 exec）：校验失败保留输入、不发 POST。"""
    rules = reminders.default_rules()
    ui = make_ui(rules=rules)
    ui.open_page(insights_ui.REMINDERS_NAV_KEY)
    dialog = _open_rules_dialog(ui)
    dialog.widgets['key'][0].setText('9bad key')
    dialog.save()
    assert dialog.validation_error == '提醒规则key无效'
    assert dialog.error_label.isVisibleTo(dialog)  # 错误显示在表单内（对话框本身未 show）
    assert dialog.widgets['key'][0].text() == '9bad key'  # 输入被保留
    assert dialog.busy is False

    dialog.widgets['key'][0].setText('good_key')
    dialog.field_boxes['到期日期'].setChecked(False)
    dialog.save()
    assert dialog.validation_error == '提醒规则日期字段不符合模块定义'
    dialog.widgets['warn_days'][0].setValue(400)
    dialog.field_boxes['到期日期'].setChecked(True)
    dialog.save()
    assert dialog.validation_error == '提醒提前天数须为0—365'
    assert ui.posts == []  # 校验失败绝不发 POST
    dialog.deleteLater()
    ui.close()


def test_rule_save_success_includes_id_and_outer_version(qapp):
    rules = [dict(reminders.default_rules()[0], id='rule-1', version=3)]
    ui = make_ui(rules=rules)
    ui.open_page(insights_ui.REMINDERS_NAV_KEY)
    rule = ui._reminders_rules[0]
    assert rule['id'] == 'rule-1' and rule['version'] == 3
    dialog = _open_rules_dialog(ui, rule=rule)

    def drive(instance):
        instance.widgets['warn_days'][0].setValue(15)
        instance.buttons.accepted.emit()
        return QDialog.Accepted

    with patch.object(insights_ui.RulesDialog, 'exec', drive):
        dialog.exec()

    assert len(ui.posts) == 1
    _, path, payload = ui.posts[0]
    assert path == '/reminders' and payload['id'] == rule['id'] and payload['version'] == rule['version']
    assert set(payload) == {'data', 'id', 'version'}
    assert list(payload['data']) == list(insights_ui.RULE_KEYS)
    assert payload['data']['warn_days'] == 15
    reminders.validate_rule(payload['data'])  # 提交内容必须能通过权威校验
    assert ui.client.calls[-1] == ('GET', '/reminders')  # 成功后重新读取并重绘
    assert '已保存' in ui.reminders_rule_message.text()
    ui.close()


def test_new_rule_omits_id_and_version_and_builds_lists(qapp):
    ui = make_ui(rules=[])
    ui.open_page(insights_ui.REMINDERS_NAV_KEY)
    dialog = _open_rules_dialog(ui, '新建提醒规则', {})

    def drive(instance):
        instance.widgets['key'][0].setText('asset_warranty_expiry')
        instance.widgets['name'][0].setText('资产维保到期提醒')
        for key in list(instance.module_boxes):
            instance.module_boxes[key].setChecked(key == 'assets')
        instance.field_boxes['维保到期日期'].setChecked(True)
        instance.buttons.accepted.emit()
        return QDialog.Accepted

    with patch.object(insights_ui.RulesDialog, 'exec', drive):
        dialog.exec()

    _, _, payload = ui.posts[0]
    assert set(payload) == {'data'}  # 新建不带 id / version
    data = payload['data']
    assert data['key'] == 'asset_warranty_expiry' and data['modules'] == ['assets']
    assert data['date_fields'] == ['维保到期日期'] and data['type'] == 'expiry'
    reminders.validate_rule(data)
    ui.close()


def test_get_failure_falls_back_to_builtin_rules_with_notice(qapp):
    ui = make_ui()
    ui.client.fail_get = True
    ui.open_page(insights_ui.REMINDERS_NAV_KEY)
    assert ui.client.calls[-1] == ('GET', '/reminders')
    assert ui._reminders_load_failed is True
    assert ui._reminders_rules == reminders.default_rules()
    assert ui._reminders_rules_table.rowCount() == len(reminders.default_rules())
    notice = ui.reminders_rule_message.text()
    assert '规则读取失败' in notice and '内置默认规则' in notice and '修改将失败' in notice
    ui.close()


def test_rules_import_preview_then_confirm(qapp, tmp_path):
    ui = make_ui(rules=[])
    ui.open_page(insights_ui.REMINDERS_NAV_KEY)
    source = tmp_path / 'rules.json'
    source.write_text(json.dumps({'rules': [dict(reminders.default_rules()[1], name='任务逾期提醒'), 'not-a-rule']},
                                 ensure_ascii=False), encoding='utf-8')
    previews, notes = [], []

    def inspect(dialog):
        editor = dialog.findChild(QPlainTextEdit)
        previews.append(editor.toPlainText() if editor is not None else '')
        notes.append('\n'.join(label.text() for label in dialog.findChildren(QLabel)))
        dialog.accept()
        return QDialog.Accepted

    with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(source), 'JSON (*.json)')), \
         patch.object(FormDialog, 'exec', inspect):
        ui._reminders_import()

    assert len(previews) == 1  # 先预览再确认
    assert '任务逾期提醒' in previews[0] and 'asset_warranty_expiry' in previews[0]
    assert '已跳过 1 条无效数据' in notes[0] and '第2条' in notes[0]  # 预览里说明跳过了哪条
    assert len(ui.posts) == 1
    _, path, payload = ui.posts[0]
    assert path == '/reminders' and set(payload) == {'data'}
    assert payload['data']['key'] == 'asset_warranty_expiry'
    reminders.validate_rule(payload['data'])
    assert ui.client.calls[-1] == ('GET', '/reminders')
    ui.close()


def test_rules_import_cancel_posts_nothing(qapp, tmp_path):
    ui = make_ui(rules=[])
    ui.open_page(insights_ui.REMINDERS_NAV_KEY)
    source = tmp_path / 'rules.json'
    source.write_text(json.dumps([reminders.default_rules()[0]], ensure_ascii=False), encoding='utf-8')
    with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(source), 'JSON (*.json)')), \
         patch.object(FormDialog, 'exec', return_value=QDialog.Rejected):
        ui._reminders_import()
    assert ui.posts == []
    assert '已取消导入' in ui.reminders_rule_message.text()
    ui.close()


def test_rules_export_writes_json(qapp, tmp_path):
    ui = make_ui()
    ui.open_page(insights_ui.REMINDERS_NAV_KEY)
    target = tmp_path / 'rules-out.json'
    with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(target), 'JSON (*.json)')):
        ui._reminders_export()
    payload = json.loads(target.read_text(encoding='utf-8'))
    assert payload['kind'] == 'reminder-rules'
    assert list(payload['rules'][0]) == list(insights_ui.RULE_KEYS)
    assert payload['rules'][0]['name'] == reminders.default_rules()[0]['name']
    assert f'已导出 {len(reminders.default_rules())} 条规则' in ui.reminders_rule_message.text()
    ui.close()


def test_save_failure_keeps_form_and_input(qapp):
    ui = make_ui()
    ui.open_page(insights_ui.REMINDERS_NAV_KEY)
    dialog = _open_rules_dialog(ui)
    dialog.widgets['name'][0].setText('改名后的提醒')
    ui.client.fail_post = True
    dialog.save()
    assert dialog.busy is False
    assert '超时' in dialog.validation_error
    assert dialog.validation_error and dialog.error_label.isVisibleTo(dialog)
    assert dialog.widgets['name'][0].text() == '改名后的提醒'
    dialog.deleteLater()
    ui.close()


# --------------------------------------------------------------------------------------
# 3 建立整改任务
# --------------------------------------------------------------------------------------
def _select_high(ui):
    ui.health_level_filter.setCurrentIndex(ui.health_level_filter.findData('高'))
    ui._health_check_all(True)


def test_health_task_payload_order_and_record_id(qapp):
    records = healthy_records()
    ui = make_ui(records)
    ui.open_page(insights_ui.HEALTH_NAV_KEY)
    FakeDialog.instances, FakeDialog.mode, FakeDialog.mutate = [], 'success', None
    with patch.object(insights_ui, 'ActionCreationDialog', FakeDialog), \
         patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
        _select_high(ui)
        ui._health_create_tasks()

    assert FakeDialog.instances
    for dialog in FakeDialog.instances:
        source = next(row for row in records if row['id'] == dialog.source_id)  # source_id 必须是真实记录 id
        expected = health.finding_task_items([f for f in health.scan(records, TODAY)['findings']
                                              if f['record_id'] == dialog.source_id])
        assert dialog.suggestions == expected
        assert list(dialog.suggestions[0]) == ['name', 'owner', 'due_date', 'next_step', 'acceptance']
        assert dialog.suggestions[0]['name'].startswith('整改：')
        assert dialog.suggestions[0]['next_step'] and dialog.suggestions[0]['acceptance']
        UUID(dialog.request_id)
        assert source['module'] in MODULES
    assert 'task-' in ui.health_task_message.text()
    assert ui.refreshes == 1
    ui.close()


def test_health_task_groups_by_record_and_reuses_request_id(qapp):
    records = [overdue_task(负责人='李工', 完成证据='已归档'),
               contract(到期日期=''),
               record('v1', 'reviews', 名称='人员考核', 人员ID='', 考核周期='', 指标口径='', 状态='未开始')]
    findings = health.scan(records, TODAY)['findings']
    expected_ids = {finding['record_id'] for finding in findings}
    assert len(expected_ids) == 3  # 三个记录各自有问题，需要分组分别提交
    ui = make_ui(records)
    ui.open_page(insights_ui.HEALTH_NAV_KEY)

    def observed_posts(start):
        """读取代理实际发出去的 POST 内容（request_id 以服务端收到的为准）。"""
        result = {}
        for call in ui.posts[start:]:
            if call[1] == '/actions/create':
                result[call[2]['source_id']] = (call[2]['request_id'], call[2]['items'])
        return result

    with patch.object(insights_ui, 'ActionCreationDialog', FakeDialog), \
         patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
        FakeDialog.instances, FakeDialog.mode, FakeDialog.mutate = [], 'success', None
        ui._health_check_all(True)
        ui._health_create_tasks()
        first = observed_posts(0)
        assert set(observed_posts(0)) == expected_ids  # 按记录分组分别提交
        assert len(first) == 3

        start = len(ui.posts)
        ui._health_check_all(True)
        ui._health_create_tasks()
        second = observed_posts(start)
        assert {key: value[0] for key, value in second.items()} == {key: value[0] for key, value in first.items()}
        assert {key: value[1] for key, value in second.items()} == {key: value[1] for key, value in first.items()}

        start = len(ui.posts)
        FakeDialog.mutate = '张工'  # 人工改写内容 → 换新幂等键
        ui._health_check_all(True)
        ui._health_create_tasks()
        changed = observed_posts(start)
        assert set(changed) == expected_ids
        for source_id, (request_id, items) in changed.items():
            assert items[0]['owner'] == '张工'
            assert request_id != first[source_id][0]  # 内容变了 → 该记录的幂等键必须换新
    FakeDialog.mutate = None
    ui.close()


def test_action_dialog_retry_same_id_then_new_id_on_change(qapp):
    """真实 ActionCreationDialog 的幂等语义：失败重试同 id、改内容换 id。"""
    client, runner = Client(), Runner()
    client.fail_post = True
    dialog = ActionCreationDialog('c1', ['整改：补齐到期日期'], client, runner, None)
    dialog.table.item(0, 0).setCheckState(Qt.Checked)
    with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
        dialog.submit()
        assert dialog.busy is False and dialog.error.text()
        first = client.calls[-1][2]['request_id']
        assert list(client.calls[-1][2]) == ['source_id', 'items', 'request_id']
        assert list(client.calls[-1][2]['items'][0]) == ['name', 'owner', 'due_date', 'next_step', 'acceptance']
        dialog.submit()
        assert client.calls[-1][2]['request_id'] == first
        dialog.table.item(0, 1).setText('张工')
        dialog.submit()
        assert client.calls[-1][2]['request_id'] != first
    dialog.deleteLater()


def test_health_task_requires_selection_and_record_id(qapp):
    ui = make_ui(healthy_records())
    ui.open_page(insights_ui.HEALTH_NAV_KEY)
    with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
        ui._health_create_tasks()  # 默认全不选
        assert '请先勾选' in ui.health_task_message.text()
        assert ui.posts == []
    ui.close()


def test_async_failure_shows_reason_and_keeps_selection(qapp):
    ui = make_ui(healthy_records())
    ui.open_page(insights_ui.HEALTH_NAV_KEY)
    FakeDialog.instances, FakeDialog.mode = [], 'failure'
    with patch.object(insights_ui, 'ActionCreationDialog', FakeDialog), \
         patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
        ui.client.fail_post = True
        _select_high(ui)
        ui._health_create_tasks()
    message = ui.health_task_message.text()
    assert '未建立任务' in message and '保留' in message and '同一请求编号' in message
    assert any(ui.health_table.item(row, 0).checkState() == Qt.Checked
               for row in range(ui.health_table.rowCount()))  # 勾选保留
    assert ui.refreshes == 0
    FakeDialog.mode = 'success'
    ui.close()


def test_stale_callback_never_touches_destroyed_widgets(qapp):
    ui = make_ui()
    ui.runner.defer = True
    ui.open_page(insights_ui.REMINDERS_NAV_KEY)
    old_table = ui.reminders_table
    assert old_table.rowCount() == 0  # 无记录 → 无提醒（GET 尚未返回）

    ui.open_page(insights_ui.HEALTH_NAV_KEY)  # 切页 → generation+1、旧控件销毁
    ui.runner.defer = False
    ui.runner.settle()  # 旧页面的 GET /reminders 此刻才返回

    assert old_table.rowCount() == 0  # 旧表未被写入
    assert ui.health_table.rowCount() == 0  # 当前页面未被旧回调污染

    ui.open_page(insights_ui.REMINDERS_NAV_KEY)
    assert ui.reminders_table.rowCount() == 0
    assert ui._reminders_rules_table.rowCount() == len(reminders.default_rules())  # 规则正常读取
    ui.close()


# --------------------------------------------------------------------------------------
# 4 导出
# --------------------------------------------------------------------------------------
def test_health_export_writes_csv_with_header(qapp, tmp_path):
    records = healthy_records()
    ui = make_ui(records)
    ui.open_page(insights_ui.HEALTH_NAV_KEY)
    target = tmp_path / '体检结果.csv'
    with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(target), 'CSV (*.csv)')):
        ui._health_export()
    with open(target, encoding='utf-8-sig', newline='') as handle:
        text = handle.read()
    findings = health.scan(records, TODAY)['findings']
    assert '等级,模块,事项,字段,问题说明,建议,记录ID' in text
    assert text.count('\r\n') == len(findings) + 1  # 表头 + 每项发现一行
    for finding in findings:
        assert finding['title'] in text and finding['suggestion'] in text
    assert f'已导出 {len(findings)} 项发现' in ui.health_task_message.text()
    ui.close()


def test_health_export_is_filtered_and_excludes_other_fields(qapp, tmp_path):
    secret = 'sk-live-should-never-be-exported'
    leaking = {'code': 'missing_date', 'severity': '高', 'module': 'contracts', 'record_id': 'c1',
               'title': '合同到期日期缺失', 'field': '到期日期', 'detail': '到期日期为空。',
               'suggestion': '请补充到期日期。', 'token': secret, 'raw_payload': {'api_key': secret}}
    records = healthy_records()
    with patch.object(insights_ui, 'scan_records', lambda rows, today=None: {
            'generated': '2026-03-15T09:00:00', 'scanned': len(rows or []), 'findings': [leaking]}):
        ui = make_ui(records)
        ui.open_page(insights_ui.HEALTH_NAV_KEY)
        assert ui.health_table.rowCount() == 1

        # 筛选后的导出：只导出当前可见行
        ui.health_level_filter.setCurrentIndex(ui.health_level_filter.findData('中'))
        empty_target = tmp_path / 'filtered.md'
        with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(empty_target), 'Markdown (*.md)')):
            ui._health_export()
        filtered = empty_target.read_text(encoding='utf-8')
        assert '合同到期日期缺失' not in filtered  # 该发现是“高”，被筛选排除
        assert '| --- |' in filtered and filtered.count('| 高 |') == 0

        ui.health_level_filter.setCurrentIndex(0)
        target = tmp_path / '体检结果.md'
        with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(target), 'Markdown (*.md)')):
            ui._health_export()
    text = target.read_text(encoding='utf-8')
    assert '| 等级 | 模块 | 事项 | 字段 | 问题说明 | 建议 | 记录ID |' in text
    assert '合同到期日期缺失' in text and '请补充到期日期。' in text
    assert secret not in text  # 白名单之外的字段（含潜在凭据）不会被导出
    assert 'api_key' not in text
    ui.close()
