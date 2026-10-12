"""AI weekly summary and meeting-minutes→tasks entry points (preview before creating)."""
from __future__ import annotations

import json
from datetime import date, timedelta

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout

from .forms import FormDialog
from .schema import MODULES


def ask(parent, text):
    return QMessageBox.question(parent, '外发授权 / 确认', text,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes


def _providers(window):
    return [row for row in getattr(window, 'config', {}).get('providers', []) if row.get('enabled', True)]


class MinutesTasksDialog(QDialog):
    """Editable table of AI-extracted tasks; nothing is created until confirmed."""

    def __init__(self, tasks, source_id, client, runner, parent=None):
        super().__init__(parent)
        self.tasks = list(tasks)
        self.source_id = source_id
        self.client = client
        self.runner = runner
        self.busy = False
        self.setWindowTitle('会议纪要转任务：人工确认')
        self.resize(960, 600)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('以下是模型从会议纪要中抽取的任务草稿。<strong>默认全部不勾选</strong>：'
                                '请逐条核对名称、负责人与期限，只有勾选的条目才会建立正式任务。'
                                '负责人只能填写已有人员记录中的姓名，否则会被服务端拒绝。'))
        rows = [t for t in self.tasks if t.get('name')]
        self.table = QPlainTextEdit()
        self.table.setPlainText(json.dumps(rows, ensure_ascii=False, indent=2))
        self.table.setReadOnly(True)
        layout.addWidget(self.table, 1)
        self.warnings = QLabel('')
        self.warnings.setWordWrap(True)
        layout.addWidget(self.warnings)
        tools = QHBoxLayout()
        layout.addLayout(tools)
        tools.addStretch()
        self.create_button = QPushButton('选择任务后建立…')
        close = QPushButton('关闭')
        tools.addWidget(self.create_button)
        tools.addWidget(close)
        close.clicked.connect(self.reject)
        self.create_button.clicked.connect(self.create)
        self.warnings.setText('需要复核的内容：' + ('；'.join(
            f"{task.get('name', '')}（{'、'.join(task.get('warnings') or ['无提示'])}）" for task in rows) or '无'))

    def create(self):
        if self.busy:
            return
        from .management_ui import ActionCreationDialog
        suggestions = [task.get('name', '') for task in self.tasks if task.get('name')]
        if not suggestions:
            return
        dialog = ActionCreationDialog(self.source_id, suggestions, self.client, self.runner, self)
        for index, task in enumerate([task for task in self.tasks if task.get('name')]):
            if index >= dialog.table.rowCount():
                break
            dialog.table.item(index, 1).setText(str(task.get('owner', '')))
            dialog.table.item(index, 2).setText(str(task.get('due_date', '')))
            dialog.table.item(index, 3).setText(str(task.get('next_step', '')))
            dialog.table.item(index, 4).setText(str(task.get('acceptance', '')))
        dialog.exec()
        self.accept()


class AIExtrasMixin:
    """Buttons on 汇报与草稿: weekly summary and minutes→tasks."""

    def _ai_extras_provider(self, title):
        providers = _providers(self)
        if not providers:
            self.error('请先在“系统设置 → 模型配置”启用一个模型，并确认服务端已保存对应凭据。')
            return None
        if len(providers) == 1:
            return providers[0]
        fields = [('provider_id', '外部模型 *', 'choice', '')]
        dialog = FormDialog(title, fields, choices={'provider_id': [(row.get('name', row['id']), row['id'])
                                                                   for row in providers]}, parent=self)
        if not dialog.exec():
            return None
        chosen = dialog.values().get('provider_id')
        return next((row for row in providers if row['id'] == chosen), None)

    def ai_weekly_summary(self):
        provider = self._ai_extras_provider('AI周报汇总：选择模型')
        if not provider:
            return
        fields = [('start', '开始日期 *', 'date', (date.today() - timedelta(days=6)).isoformat()),
                  ('end', '结束日期 *', 'date', date.today().isoformat()),
                  ('owner', '负责人（空为全部）', 'text', '')]
        dialog = FormDialog('AI周报汇总：范围与授权', fields, parent=self)
        hint = QLabel('将把该时间范围内的记录摘要发送给所选模型，生成待审核草稿。'
                      '不发送附件原文，不自动批准、不自动发送；人员/考核/权限资料需要二次授权。')
        hint.setWordWrap(True)
        dialog.layout().insertWidget(1, hint)
        if not dialog.exec():
            return
        values = dialog.values()
        sensitive = any(row.get('module') in ('people', 'reviews', 'access') for row in self.records
                        if str(row.get('updated', ''))[:10] >= values['start'])
        if sensitive and not ask(self, '该范围的记录可能包含人员、考核或权限资料，确认授权发送给模型？'):
            return
        payload = dict(values, provider_id=provider['id'], confirmed=True, sensitive_confirmed=sensitive)

        def done(value):
            draft = value.get('draft', value)
            self.refresh()
            self.draft_editor(draft)

        self.statusBar().showMessage('正在生成周报草稿…')
        self.runner.submit(lambda: self.client.post('/ai/weekly-summary', payload), done, self.error)

    def ai_minutes_tasks(self):
        provider = self._ai_extras_provider('会议纪要转任务：选择模型')
        if not provider:
            return
        dialog = FormDialog('会议纪要转任务', [('minutes', '会议纪要正文 *', 'long', '')], parent=self)
        hint = QLabel('粘贴会议纪要后，模型只做任务抽取，不会自动建任务：'
                      '结果先进入人工确认窗口，逐条勾选并补充负责人/期限后才会建立。'
                      '负责人必须是已有人员记录中的姓名。')
        hint.setWordWrap(True)
        dialog.layout().insertWidget(1, hint)
        if not dialog.exec():
            return
        minutes = dialog.values().get('minutes', '').strip()
        if not minutes:
            self.error('请填写会议纪要正文。')
            return
        payload = {'minutes': minutes, 'provider_id': provider['id'], 'confirmed': True}

        def done(value):
            tasks = value.get('tasks', [])
            if not tasks:
                self.error('未从纪要继续识别出可执行任务，请补充“谁、做什么、何时完成”。')
                return
            source_id = value.get('source_id') or ('ai:' + str(value.get('call_id', '')))
            MinutesTasksDialog(tasks, source_id, self.client, self.runner, self).exec()

        self.statusBar().showMessage('正在从会议纪要抽取任务…')
        self.runner.submit(lambda: self.client.post('/ai/minutes-tasks', payload), done, self.error)
