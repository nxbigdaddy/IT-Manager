"""PySide6 desktop client. Every HTTP operation runs in QThreadPool."""
from __future__ import annotations
import argparse
import json
import re
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QDialog, QVBoxLayout,
    QHBoxLayout, QFormLayout, QPushButton, QLabel, QLineEdit, QPlainTextEdit,
    QComboBox, QCheckBox, QSpinBox, QDoubleSpinBox, QScrollArea, QDialogButtonBox,
    QListWidget, QStackedWidget, QTableWidget, QTableWidgetItem, QAbstractItemView,
    QMessageBox, QFileDialog, QSplitter, QInputDialog)
from .schema import MODULES, STATUS
from .forms import FormDialog, RecordDialog
from .workspace_ui import WorkspaceMixin
from .management_ui import ManagementMixin
from .checklists_ui import ChecklistsMixin
from .workspaces_ui import WorkspaceSystemsMixin
from .charts_ui import ChartsMixin
from .insights_ui import InsightsMixin
from .batch_ui import BatchMixin
from .ai_extras_ui import AIExtrasMixin
from .tray_notifications import TrayController
from .client import APIClient, ClientError, ai_payload, export_data, import_data


class JobSignals(QObject):
    done = Signal(object, object)


class Job(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = JobSignals()

    @Slot()
    def run(self):
        try:
            result = self.function()
            self.signals.done.emit(result, None)
        except Exception as exc:
            message = str(exc) if isinstance(exc, ClientError) else '操作失败，请检查输入、依赖或本地文件权限。'
            self.signals.done.emit(None, message)


class AsyncRunner(QObject):
    """Own callbacks on the GUI thread, and retain workers until completion."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self.jobs = {}

    def submit(self, function, success, error):
        job = Job(function)
        self.jobs[job.signals] = (job, success, error)
        job.signals.done.connect(self.finished)
        self.pool.start(job)

    @Slot(object, object)
    def finished(self, value, error):
        item = self.jobs.pop(self.sender(), None)
        if item:
            try:
                (item[2] if error else item[1])(error if error else value)
            except Exception:
                # Both result and error targets can have been destroyed on navigation.
                if not error:
                    try:item[2]('页面结果显示失败，请刷新核对；数据可能已经保存。')
                    except Exception:pass


def button(text, callback, layout):
    widget = QPushButton(text)
    widget.clicked.connect(callback)
    layout.addWidget(widget)
    return widget


def ask(parent, text):
    return QMessageBox.question(parent, '确认操作', text,
        QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes


def show_data(parent, title, value):
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(800, 600)
    layout = QVBoxLayout(dialog)
    text = QPlainTextEdit(json.dumps(value, ensure_ascii=False, indent=2) if not isinstance(value, str) else value)
    text.setReadOnly(True)
    layout.addWidget(text)
    button('关闭', dialog.accept, layout)
    dialog.exec()


RELATIONS = {'规划ID': 'plans', '项目ID': 'projects', '父任务ID': 'tasks', '人员ID': 'people',
             '系统ID': 'systems', '整改任务ID': 'tasks', '后续任务ID': 'tasks'}
LONG_WORDS = ('内容', '证据', '说明', '评价', '反馈', '目标', '标准', '备注', '过程', '措施', '计划', '复盘', '职责', '安排', '原因', '下一步', '风险', '对比', '建议', '步骤')


class DraftDialog(QDialog):
    busy = False
    def reject(self):
        if self.busy:
            QMessageBox.information(self,'请求处理中','请等待请求完成；关闭窗口不会取消发送。')
            return
        if hasattr(self,'is_dirty') and self.is_dirty() and not ask(self,'正文尚未保存，确认放弃修改？'):
            return
        super().reject()


CONFIG_FIELDS = {
 'providers': [('name','名称','text',''), ('provider','平台',['deepseek','kimi','qwen'],'deepseek'),
  ('model','模型','text','deepseek-chat'), ('base_url','官方HTTPS地址','text','https://api.deepseek.com/v1'),
  ('api_key_env','API密钥引用名称','text','DEEPSEEK_API_KEY'), ('temperature','温度','float',0.7),
  ('max_tokens','输出上限','int',2048), ('timeout','超时秒','int',60), ('enabled','启用','bool',True)],
 'channels': [('name','名称','text',''), ('type','类型',['wecom','feishu','dingtalk','email'],'wecom'),
  ('webhook_env','Webhook凭据引用','text',''), ('secret_env','签名凭据引用（可空）','text',''),
  ('host','SMTP主机','text',''), ('port','SMTP端口','int',465), ('security','邮件加密',['ssl','starttls'],'ssl'),
  ('username_env','SMTP用户名引用','text',''), ('password_env','SMTP密码引用','text',''),
  ('from_addr','发件邮箱','text',''), ('to_addrs','收件邮箱（逗号分隔）','text',''), ('enabled','启用','bool',True)],
 'prompts': [('name','名称','text',''), ('category','分类','text',''), ('purpose','用途（draft通用 / form_fill填表）',['draft','form_fill'],'draft'), ('module','适用业务模块','text',''), ('is_default','设为该模块填表默认模板','bool',False), ('template','模板（填表变量：basis/current/context/schema/today）','long','请基于材料生成待审核草稿：\n{{content}}'),
  ('system','系统约束','long','仅整理事实；缺失信息标注待确认；不得自动评级或执行操作。'), ('enabled','启用','bool',True)],
 'schedules': [('name','名称','text',''), ('kind','类型（backup为一致性备份计划）',['report','reminder','backup'],'report'),
  ('mode','模式',['draft','auto_send'],'draft'), ('channel_id','目标渠道','text',''), ('owner','负责人范围','text',''),
  ('period_days','报告周期天数','int',7), ('frequency','频率',['daily','weekly','monthly'],'weekly'),
  ('timezone','IANA时区','text','Asia/Shanghai'), ('hour','小时0—23','int',9), ('minute','分钟0—59','int',0),
  ('weekday','周几（周一0）','int',0), ('day','月几1—31（备份计划按28以内计算）','int',1), ('attachment','邮件附件','bool',False), ('enabled','启用','bool',False),
  ('keep','备份保留份数（仅建议清理，不自动删除）','int',7), ('scope','备份范围说明','text','全部业务库')]}


class MainWindow(AIExtrasMixin, BatchMixin, InsightsMixin, ChartsMixin, WorkspaceSystemsMixin, ManagementMixin, ChecklistsMixin, WorkspaceMixin, QMainWindow):
    def __init__(self, client, autoload=True):
        super().__init__()
        self.client = client
        self.runner = AsyncRunner(self)
        self.records = []
        self.config = {k: [] for k in CONFIG_FIELDS}
        self.current = 'dashboard'
        self.generation = 0
        self.setup_workspace()
        self.tray = TrayController(self, lambda: self.jump_to('reminders'))
        self.tray.start(self.tray_items)
        if autoload:
            self.refresh()

    def error(self, message):
        self.statusBar().showMessage(message)
        QMessageBox.warning(self, '操作未完成', message)

    def run(self, work, success=None):
        self.statusBar().showMessage('正在处理…（界面仍可操作）')
        def complete(value):
            self.statusBar().showMessage('操作完成')
            if success: success(value)
        self.runner.submit(work, complete, self.error)

    def refresh(self):
        client = self.client
        self.refresh_serial=getattr(self,'refresh_serial',0)+1
        serial=self.refresh_serial
        def load():
            return client.get('/health'), client.get('/records'), {k:client.get('/objects/'+k) for k in CONFIG_FIELDS}
        def loaded(value):
            if client is not self.client or serial!=self.refresh_serial:return
            health, self.records, self.config = value
            from . import __version__
            server_version=str(health.get('version','未知'))
            mismatch=server_version!=__version__
            place='本地工作库' if client.url.startswith('http://127.0.0.1') else client.url
            self.connection.setText(f"已连接 · {place} · v{server_version}" + (' ⚠版本不一致' if mismatch else ''))
            self.connection.setToolTip(f"客户端 v{__version__}；服务端 v{server_version}；调度器：{'开启' if health.get('scheduler') else '关闭'}；刷新保留筛选条件")
            if mismatch:
                self.statusBar().showMessage(f'客户端 v{__version__} 与服务端 v{server_version} 不一致，请同时重启桌面和后台服务。')
            self.navigate(self.nav.currentRow())
        def failed(message):
            if client is self.client and serial==self.refresh_serial:
                self.connection.setText('连接失败 · 可点击刷新重试')
                self.error(message)
        self.runner.submit(load,loaded,failed)

    def clear(self):
        def empty(layout):
            while layout.count():
                item=layout.takeAt(0)
                if item.widget():item.widget().deleteLater()
                elif item.layout():empty(item.layout());item.layout().deleteLater()
        empty(self.page_layout)

    def navigate(self, index):
        if index < 0: return
        self.generation += 1
        self.current = self.keys[index]
        self.clear()
        title = QLabel(self.nav.item(index).text())
        title.setStyleSheet('font-size:22px;font-weight:bold;padding:6px')
        self.page_layout.addWidget(title)
        if self.current in MODULES: self.record_page()
        elif self.current in CONFIG_FIELDS: self.config_page()
        elif self.current == 'dashboard': self.dashboard()
        elif self.current == 'checklists': self.checklists_page()
        elif self.current == 'workspaces': self.workspaces_page()
        elif self.current == 'reminders' and hasattr(self,'reminders_page'): self.reminders_page()
        elif self.current == 'health' and hasattr(self,'health_page'): self.health_page()
        elif self.current == 'charts' and hasattr(self,'charts_page'): self.charts_page()
        elif self.current == 'drafts': self.drafts_page()
        elif self.current == 'ai': self.ai_page()
        elif self.current == 'system': self.system_page()
        else:
            path = {'ai_calls':'/ai/calls','deliveries':'/deliveries','audit':'/audit'}[self.current]
            generation = self.generation
            self.run(lambda:self.client.get(path), lambda rows:self.generic_table(rows) if generation == self.generation else None)

    def selected(self, table):
        item = table.item(table.currentRow(), 0)
        return item.data(Qt.UserRole) if item else None

    def generic_table(self, rows):
        if not isinstance(rows, list):
            show_data(self, '结果', rows)
            return
        fields={
            'audit':[('time','时间'),('action','操作'),('object_id','关联对象'),('detail','说明')],
            'deliveries':[('state','结果'),('title','标题'),('attempted','发送时间'),('error','提示')],
            'ai_calls':[('time','时间'),('provider','平台'),('model','模型'),('status','结果')],
        }.get(self.current,[(key,key) for key in list(dict.fromkeys(k for row in rows for k in row))[:5]])
        table=self.table([label for _,label in fields] or ['暂无记录'])
        names={'sent':'发送成功','uncertain':'结果不确定，请核对','blocked':'已阻止','failed':'失败','draft_created':'已生成草稿','pending':'等待发送','sending':'正在发送','success':'成功'}
        def value(row,key):
            val=row.get(key,row.get('data',{}).get(key,''))
            return names.get(str(val),str(val))
        self.fill(table,rows,[lambda r,k=k:value(r,k) for k,_ in fields])
        if not rows:self.page_layout.addWidget(QLabel('暂无记录。完成相关操作后，会在这里留下可追溯的结果。'))
        table.doubleClicked.connect(lambda:show_data(self,'完整记录',self.selected(table)))

    def persist_dialog(self, dialog, work):
        # Keep editor open on errors. Disable Save while the asynchronous request runs.
        try: dialog.buttons.accepted.disconnect()
        except RuntimeError: pass
        def save():
            if hasattr(dialog,'validate') and not dialog.validate():return
            try:data=dialog.values()
            except Exception:
                dialog.show_error('请检查当前表单的输入格式。')
                return
            if hasattr(dialog,'set_busy'):dialog.set_busy(True)
            else:dialog.buttons.setEnabled(False)
            def done(value):
                if hasattr(dialog,'set_busy'):dialog.set_busy(False)
                dialog.accept()
                self.refresh()
            def failed(message):
                if hasattr(dialog,'set_busy'):dialog.set_busy(False)
                else:dialog.buttons.setEnabled(True)
                if hasattr(dialog,'show_error'):dialog.show_error(message)
                else:self.error(message)
            self.runner.submit(lambda:work(data), done, failed)
        dialog.buttons.accepted.connect(save)
        dialog.exec()

    def edit_record(self, module, record=None):
        dialog = RecordDialog(module, record, self.records, self)
        from .ai_form_ui import attach_ai_assistant
        attach_ai_assistant(dialog,module,self.records,self.config['providers'],self.config['prompts'],self.runner,self.client)
        def save(data):
            if not data.get(MODULES[module][1][0]): raise ClientError('名称或姓名必填。')
            for field,value in data.items():
                if '日期' in field and value:
                    try:
                        if date.fromisoformat(value).isoformat()!=value: raise ValueError
                    except (ValueError,TypeError): raise ClientError(field+'须使用YYYY-MM-DD格式。') from None
            return self.client.post('/records/'+module, {'data':data, 'id':(record or {}).get('id'), 'version':(record or {}).get('version')})
        self.persist_dialog(dialog, save)

    def archive(self, record):
        if not record:
            self.statusBar().showMessage('请先选择需要归档的记录。')
            return
        if ask(self,'归档此记录？有关联引用的记录将由服务端拒绝归档。'):
            client=self.client
            self.run(lambda:client.request('DELETE',f"/records/{record['id']}?version={record['version']}"),lambda _:self.refresh() if client is self.client else None)

    def config_page(self):
        kind = self.current
        tools = QHBoxLayout(); self.page_layout.addLayout(tools)
        table = self.table(['名称','状态','配置摘要'])
        rows = self.config[kind]
        def summary(row):
            if kind=='prompts':
                module_title=MODULES.get(row.get('module'),('通用草稿',[]))[0]
                return f"{module_title} · v{row.get('version',1)}"+(' · 默认填表模板' if row.get('is_default') else '')
            return row.get('model',row.get('next_run',row.get('type','')))
        def show_rows(selected=None):
            visible=rows if not selected else [r for r in rows if r.get('module')==selected]
            self.fill(table,visible,[lambda r:r.get('name',''),lambda r:'已启用' if r.get('enabled',True) else '已停用',summary])
        show_rows()
        if kind=='prompts':
            module_filter=QComboBox();module_filter.addItem('全部提示词','')
            for key,(title,_) in MODULES.items():module_filter.addItem(title,key)
            tools.addWidget(module_filter)
            module_filter.currentIndexChanged.connect(lambda _:show_rows(module_filter.currentData()))
        if not rows:
            self.page_layout.addWidget(QLabel('还没有配置。点击“新建”开始；模型和提示词也可以使用“添加默认模板”。'))
        button('新建',lambda:self.edit_config(kind),tools)
        button('编辑',lambda:self.edit_config(kind,self.selected(table)) if self.selected(table) else None,tools)
        button('启用 / 停用',lambda:self.toggle_config(kind,self.selected(table)),tools)
        table.doubleClicked.connect(lambda:self.edit_config(kind,self.selected(table)))
        if kind in ('providers','channels'):
            button('测试连接' if kind=='providers' else '发送测试消息',lambda:self.test_config(kind,self.selected(table)),tools)
        if kind=='prompts':
            button('历史版本',self.prompt_history,tools)
            def copy_prompt():
                row=self.selected(table)
                if not row:return
                data=dict(row);data.pop('id',None);data.pop('version',None)
                data['name']=data.get('name','')+'（副本）'
                data['is_default']=False
                self.edit_config('prompts',data)
            button('复制提示词',copy_prompt,tools)
        if kind in ('providers','prompts'):
            button('添加默认模板',lambda:self.add_defaults(kind),tools)

    def edit_config(self, kind, row=None):
        values = dict(row or {})
        if kind=='schedules': values.update(values.get('schedule',{}))
        if isinstance(values.get('to_addrs'),list): values['to_addrs'] = ', '.join(values['to_addrs'])
        choices = {'channel_id':[(r['name'],r['id']) for r in self.config['channels']]} if kind=='schedules' else ({'module':[(title,key) for key,(title,_) in MODULES.items()]} if kind=='prompts' else {})
        dialog = FormDialog('配置', CONFIG_FIELDS[kind], values, choices, self)
        if kind=='prompts':
            hint=QLabel('填表模板：用途选择form_fill并指定模块；可用变量basis、current、context、schema、today。\n模型必须输出JSON对象，包含fields（字段名→文本）、suggestions和warnings（文本列表）。通用草稿仍使用content/records。')
            hint.setWordWrap(True);dialog.layout().insertWidget(1,hint)
        def save(data):
            data = dict(data)
            data.pop('id',None)
            if kind=='schedules':
                data['schedule'] = {k:data.pop(k) for k in ('frequency','timezone','hour','minute','weekday','day')}
            if kind=='channels':
                if data.get('type')=='email':data['to_addrs'] = [s.strip() for s in data.get('to_addrs','').split(',') if s.strip()]
                data = {k:v for k,v in data.items() if v != ''}
            return self.client.post('/objects/'+kind, {'id':(row or {}).get('id'),'data':data})
        # Auto-send authorization must happen on GUI thread, not worker.
        if kind=='schedules':
            mode = dialog.widgets['mode'][0]
            mode.currentTextChanged.connect(lambda text: self._confirm_auto(mode) if text=='auto_send' else None)
        self.persist_dialog(dialog, save)

    def _confirm_auto(self, combo):
        if not ask(self,'自动发送将按计划无需逐次批准而实际发送报告。确认预先授权？请核对渠道、范围、时区后启用。'):
            combo.setCurrentIndex(0)

    def toggle_config(self, kind, row):
        if not row:
            self.statusBar().showMessage('请先选择需要启用或停用的配置。')
            return
        if kind=='schedules' and not row.get('enabled',False) and row.get('mode')=='auto_send' and not ask(self,'启用预授权自动发送计划？将实际发送到配置渠道。'): return
        data = dict(row); data['enabled'] = not row.get('enabled',True); data.pop('id',None)
        client=self.client
        self.run(lambda:client.post('/objects/'+kind,{'id':row['id'],'data':data}),lambda _:self.refresh() if client is self.client else None)

    def test_config(self, kind, row):
        if not row:
            self.statusBar().showMessage('请先选择要测试的配置。')
            return
        text = '测试将调用外部模型，可能产生费用。确认？' if kind=='providers' else '测试将向以下渠道实际发送消息，确认？\n'+row.get('name','')+'\n'+str(row.get('to_addrs',''))
        if not ask(self,text): return
        path,key = ('/ai/test','provider_id') if kind=='providers' else ('/channels/test','channel_id')
        client=self.client
        self.run(lambda:client.post(path,{key:row['id']}),lambda value:show_data(self,'测试结果',value))

    def add_defaults(self, kind):
        def loaded(defaults):
            options = list(defaults[kind])
            name,ok = QInputDialog.getItem(self,'默认配置','选择',options,0,False)
            if ok:
                data = dict(defaults[kind][name],provider=name,enabled=True) if kind=='providers' else dict(name=name,template=defaults[kind][name],enabled=True)
                self.edit_config(kind,data)
        self.run(lambda:self.client.get('/defaults'),loaded)

    def prompt_history(self):
        def loaded(rows):
            if not rows: show_data(self,'提示词历史','暂无历史'); return
            dialog = QDialog(self); dialog.setWindowTitle('提示词历史'); dialog.resize(800,600)
            layout=QVBoxLayout(dialog); choices=QComboBox(); layout.addWidget(choices)
            for row in rows: choices.addItem(f"{row.get('name')} · v{row.get('version')}",row)
            preview=QPlainTextEdit(); preview.setReadOnly(True); layout.addWidget(preview)
            def update(): preview.setPlainText(choices.currentData().get('template',''))
            choices.currentIndexChanged.connect(update); update()
            def copy():
                row=dict(choices.currentData()); row.pop('id',None); row.pop('version',None)
                dialog.accept(); self.edit_config('prompts',row)
            button('复制为新提示词',copy,layout); dialog.exec()
        self.run(lambda:self.client.get('/prompt-history'),loaded)

    def drafts_page(self):
        tools=QHBoxLayout(); self.page_layout.addLayout(tools)
        button('生成范围报告',self.report_dialog,tools)
        button('新建邮件 / 任务下达草稿',self.new_draft,tools)
        if hasattr(self,'ai_weekly_summary'):
            button('AI周报汇总…',self.ai_weekly_summary,tools)
            button('会议纪要转任务…',self.ai_minutes_tasks,tools)
        table=self.table(['标题','状态','来源','创建时间'])
        generation=self.generation
        self.run(lambda:self.client.get('/objects/drafts'),lambda rows:self.fill(table,rows,[lambda r:r.get('title',''),lambda r:r.get('status',''),lambda r:r.get('source',''),lambda r:r.get('created','')]) if generation==self.generation else None)
        button('编辑 / 批准 / 发送',lambda:self.draft_editor(self.selected(table)) if self.selected(table) else None,tools)
        table.doubleClicked.connect(lambda:self.draft_editor(self.selected(table)))

    def report_dialog(self):
        fields=[('title','标题','text','工作汇报'),('start','开始日期','date',(date.today()-timedelta(days=6)).isoformat()),('end','结束日期','date',date.today().isoformat()),('owner','负责人（空为全部）','text','')]
        fields += [(m,title,'bool',True) for m,(title,_) in MODULES.items()]
        dialog=FormDialog('生成报告范围',fields,parent=self)
        def save(data):
            selected=[m for m in MODULES if data.pop(m)]
            data['modules']=selected
            return self.client.post('/reports',data)
        self.persist_dialog(dialog,save)

    def new_draft(self):
        dialog=FormDialog('新建草稿',[('title','标题','text',''),('content','正文','long','')],parent=self)
        self.persist_dialog(dialog,lambda data:self.client.post('/drafts',data))

    def draft_editor(self, draft):
        if not draft: return
        dialog=DraftDialog(self); dialog.setWindowTitle(draft.get('title','草稿')); dialog.resize(900,700)
        layout=QVBoxLayout(dialog); content=QPlainTextEdit(draft.get('content','')); layout.addWidget(content)
        status=QLabel('当前状态：'+draft.get('status','')); layout.addWidget(status)
        channel=QComboBox()
        for row in self.config['channels']:
            if row.get('enabled',True): channel.addItem(row['name']+' · '+str(row.get('to_addrs',row.get('type',''))),row)
        layout.addWidget(channel)
        attachment=QCheckBox('附带报告文件（仅邮件）'); layout.addWidget(attachment)
        controls=QHBoxLayout(); layout.addLayout(controls)
        saved={'content':draft.get('content',''),'status':draft.get('status',''),'request_id':None,'revision':draft.get('revision',1)}
        dialog.is_dirty=lambda:content.toPlainText()!=saved['content']
        def review(approved):
            text=content.toPlainText()
            if approved and not ask(dialog,'确认已人工审核当前正文并批准此版本？'): return
            payload={'content':text,'approved':approved,'revision':saved['revision']}
            dialog.busy=True
            for widget in (save_button,approve_button,send_button):widget.setEnabled(False)
            def unlock():
                dialog.busy=False
                for widget in (save_button,approve_button,send_button):widget.setEnabled(True)
            def done(value):
                saved.update(content=text,status='approved' if approved else 'draft',request_id=None,revision=value.get('revision',saved['revision']))
                status.setText('已批准' if approved else '已保存草稿（未批准）');unlock()
            def failed(message):unlock();self.error(message)
            self.runner.submit(lambda:self.client.post('/drafts/'+draft['id']+'/review',payload),done,failed)
        save_button=button('保存草稿',lambda:review(False),controls)
        approve_button=button('审核批准',lambda:review(True),controls)
        def send():
            if saved['status']!='approved' or saved['content']!=content.toPlainText(): self.error('正文已修改或未批准，请先保存并重新审核。'); return
            target=channel.currentData()
            if not target: self.error('请先配置并启用渠道。'); return
            if not ask(dialog,'即将实际发送批准正文至：\n'+channel.currentText()+'\n结果不确定时请先核对平台和发送记录。'): return
            if saved['request_id'] and not ask(dialog,'此窗口已发起过发送。确认已核对平台结果，并需要再次发送？'): return
            saved['request_id']=uuid.uuid4().hex
            payload={'draft_id':draft['id'],'draft_revision':saved['revision'],'channel_id':target['id'],'request_id':saved['request_id'],'attachment':attachment.isChecked()}
            dialog.busy=True
            for widget in (save_button,approve_button,send_button):widget.setEnabled(False)
            def unlock():
                dialog.busy=False
                for widget in (save_button,approve_button,send_button):widget.setEnabled(True)
            def done(value): unlock(); show_data(dialog,'发送结果',value)
            def failed(message): unlock(); self.error(message)
            self.runner.submit(lambda:self.client.post('/send',payload),done,failed)
        send_button=button('发送',send,controls)
        def exported():
            path,_=QFileDialog.getSaveFileName(dialog,'导出正文','','Markdown (*.md);;Text (*.txt)')
            if path:
                text=content.toPlainText(); self.run(lambda:Path(path).write_text(text,encoding='utf-8'))
        button('导出正文',exported,controls)
        if draft.get('source')=='ai':
            layout.addWidget(QLabel('AI仅提供建议，不自动执行。任务拆解请逐条核实负责人、期限及验收条件后手工创建。'))
            button('根据建议手工新建任务',lambda:self.edit_record('tasks'),controls)
        content.textChanged.connect(lambda:status.setText('正文已修改：必须重新批准') if content.toPlainText()!=saved['content'] else None)
        dialog.exec()
        self.refresh()

    def ai_page(self):
        layout=self.page_layout
        layout.addWidget(QLabel('仅将勾选的记录发送给外部模型；人员、考核、权限需要二次确认。输出仅为草稿。'))
        provider=QComboBox(); prompt=QComboBox()
        for row in self.config['providers']:
            if row.get('enabled',True): provider.addItem(row.get('name',row.get('model','')),row)
        for row in self.config['prompts']:
            if row.get('enabled',True) and row.get('purpose','draft')=='draft': prompt.addItem(row.get('name',''),row)
        layout.addWidget(provider); layout.addWidget(prompt)
        table=self.table(['选择','模块','名称','ID'])
        table.setSortingEnabled(False); table.setRowCount(len(self.records))
        snapshot=list(self.records)
        for i,row in enumerate(snapshot):
            check=QTableWidgetItem(); check.setFlags(Qt.ItemIsEnabled|Qt.ItemIsUserCheckable); check.setCheckState(Qt.Unchecked); table.setItem(i,0,check)
            for j,text in enumerate([MODULES[row['module']][0],str(next(iter(row['data'].values()),'')),row['id']],1): table.setItem(i,j,QTableWidgetItem(text))
        def generate():
            records=[row for i,row in enumerate(snapshot) if table.item(i,0).checkState()==Qt.Checked]
            if not records or len(records)>100 or not provider.currentData() or not prompt.currentData(): self.error('请选择1—100条记录及已启用模型/提示词。'); return
            show_data(self,'即将提交的完整记录（请核对字段）',records)
            if not ask(self,'确认将刚预览的完整选定记录提交至 '+provider.currentText()+'？可能产生费用。'): return
            sensitive=any(r['module'] in ('people','reviews','access') for r in records)
            if sensitive and not ask(self,'二次授权：选定资料包含人员、考核或权限信息，确认允许向外部模型披露？'): return
            variables={}
            for key in set(re.findall(r'{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}',prompt.currentData().get('template','')))-{'content','records'}:
                value,ok=QInputDialog.getMultiLineText(self,'提示词变量',key)
                if not ok:return
                variables[key]=value
            payload=ai_payload(records,provider.currentData()['id'],prompt.currentData()['id'],variables,sensitive)
            client=self.client;generation=self.generation
            generate_button.setEnabled(False)
            self.statusBar().showMessage('AI正在生成，切换页面不会取消模型调用；请勿重复提交。')
            def finished(value):
                if generation==self.generation:
                    generate_button.setEnabled(True)
                    self.draft_editor(value)
                else:self.statusBar().showMessage('AI草稿已生成，请到“汇报与草稿”查看。')
            def failed(message):
                if generation==self.generation:generate_button.setEnabled(True)
                self.error(message)
            self.runner.submit(lambda:client.post('/ai/generate',payload),finished,failed)
        generate_button=button('预览、确认并生成AI草稿',generate,layout)
        if not provider.count() or not prompt.count():
            generate_button.setEnabled(False)
            layout.addWidget(QLabel('开始前请在系统设置中添加模型配置与提示词；模型密钥在凭据管理中保存。'))

    def system_page(self):
        layout=self.page_layout
        layout.addWidget(QLabel('本客户端不保存明文令牌。凭据提交至服务端受控存储；配置表只保存引用名称。\n导出包含人员/考核等敏感业务资料，请安全保管。Excel导入按模块工作表和字段表头映射，列值优先于JSON备份。'))
        button('新增 / 更新服务端凭据',self.secret_dialog,layout)
        button('查看凭据名称（不读取值）',lambda:self.run(lambda:self.client.get('/secrets'),lambda v:show_data(self,'凭据名称',v)),layout)
        button('服务端一致性备份',lambda:self.run(lambda:self.client.post('/backup'),lambda v:show_data(self,'服务端备份文件（非本地下载）',v)),layout)
        button('完整备份包（数据库＋附件＋凭据密文）',self.backup_bundle,layout)
        button('查看并校验备份包',self.backup_bundles,layout)
        button('备份健康与保留建议',self.backup_status,layout)
        button('完整导出 JSON / Excel',self.export_all,layout)
        button('导入 JSON / 系统导出Excel（先预览）',self.import_all,layout)
        layout.addStretch()

    def backup_bundle(self):
        if not ask(self,'将创建数据库＋附件＋可选DPAPI凭据密文的完整ZIP备份包。凭据密文仍依赖原Windows账号/系统条件解密，确认创建？'):return
        self.run(lambda:self.client.post('/backup/bundle'),
                 lambda value:show_data(self,'完整备份包（服务端文件）',value))

    def backup_bundles(self):
        def loaded(rows):
            if not rows:show_data(self,'完整备份包',{'说明':'暂无备份包。'});return
            names=[row.get('name') for row in rows if row.get('name')]
            name,ok=QInputDialog.getItem(self,'备份包校验','选择服务端备份包',names,0,False)
            if not ok:return
            self.run(lambda:self.client.post('/backup/bundle/inspect',{'name':name}),
                     lambda value:show_data(self,'备份包校验结果',value))
        self.run(lambda:self.client.get('/backup/bundles'),loaded)

    def backup_status(self):
        def loaded(value):
            health=value.get('health',{}) if isinstance(value,dict) else {}
            lines=['备份状态：' + ('正常' if health.get('ok') else '需要处理'),
                   '原因：' + ('；'.join(health.get('reasons',[])) or '无'),
                   '最新备份：' + str(health.get('newest') or '无') + f"（{health.get('age_days')} 天前）",
                   '错过的备份时刻：' + str(health.get('missed',0)),
                   '备份计划：' + (str((value.get('schedule') or {}).get('name','未配置')) +
                                '｜下次：' + str((value.get('schedule') or {}).get('next_run','')))
                   if isinstance(value,dict) else '备份计划：未配置',
                   '保留建议（仅建议，不会自动删除任何文件）：']
            delete=(value.get('retention') or {}).get('delete',[]) if isinstance(value,dict) else []
            lines += ['  · '+str(item.get('name','')) for item in delete] or ['  · 暂无可清理项']
            lines.append('最近计划备份记录：')
            for run in (value.get('runs') or []) if isinstance(value,dict) else []:
                lines.append('  · ' + str(run.get('time','')) + ' ' + str(run.get('file','')) +
                             ('（正常）' if run.get('ok') else '（异常：' + '；'.join(run.get('reasons',[])) + '）'))
            show_data(self,'备份健康检查',{'说明':'\n'.join(lines),'原始数据':value})
        self.run(lambda:self.client.get('/backup/status'),loaded)

    def secret_dialog(self):
        dialog=FormDialog('服务端安全凭据',[('name','引用名称','text',''),('value','凭据值（不会写入客户端配置）','password','')],parent=self)
        def save(data): return self.client.post('/secrets',data)
        self.persist_dialog(dialog,save)
        dialog.widgets['value'][0].clear()

    def export_all(self):
        if not ask(self,'完整导出包含人员、考核、权限等敏感记录，确认保存至本机？'):return
        path,kind=QFileDialog.getSaveFileName(self,'完整导出','itmanager-export.json','JSON (*.json);;Excel (*.xlsx)')
        if not path:return
        if not Path(path).suffix:path += '.xlsx' if 'xlsx' in kind else '.json'
        self.run(lambda:export_data(path,self.client.get('/export')),lambda _:QMessageBox.information(self,'导出','已保存至所选本地文件。'))

    def import_all(self):
        path,_=QFileDialog.getOpenFileName(self,'选择导入文件','','JSON / Excel (*.json *.xlsx)')
        if not path:return
        def preview():
            payload=import_data(path)
            return payload,self.client.post('/import/preview',payload)
        def loaded(value):
            payload,result=value
            show_data(self,'导入预览',result)
            if result.get('valid') and ask(self,'确认提交预览中的新增记录？重复ID将拒绝，不会覆盖更新。建议先备份。'):
                self.run(lambda:self.client.post('/import',payload),lambda _:self.refresh())
        self.run(preview,loaded)

    def connect_settings(self):
        if self.runner.jobs:
            self.error('当前仍有操作处理中，请完成后再切换工作库，避免把旧记录提交到新服务。')
            return
        url,ok=QInputDialog.getText(self,'服务连接','服务URL（远程必须HTTPS）',text=self.client.url)
        if not ok:return
        path,_=QFileDialog.getOpenFileName(self,'选择管理员令牌文件')
        if not path:return
        try:self.client=APIClient.from_token_file(url,path)
        except ClientError as exc:self.error(str(exc));return
        self.records=[];self.config={k:[] for k in CONFIG_FIELDS}
        self.view_state={};self.quick_filter=None
        self.navigate(self.nav.currentRow())
        self.connection.setText('正在连接新工作库…')
        self.refresh()

    def help(self):
        path=Path(__file__).resolve().parent.parent/'manual'/'index.html'
        if not path.is_file():self.error('未找到manual/index.html，请安装完整手册。');return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def tray_items(self):
        """High-level active reminders for the optional local tray; no network or evidence text."""
        try:
            from .reminders import compute_items,default_rules
            from .reminder_state import apply_states
            rules=getattr(self,'reminder_rules',None) or getattr(self,'_reminders_rules',None) or default_rules()
            items=compute_items(self.records,[r for r in rules if r.get('enabled',True)],date.today())
            versions={row.get('id'):row.get('version') for row in self.records}
            for item in items:
                item['record_version']=versions.get(item.get('record_id'))
            return apply_states(items,getattr(self,'_reminder_states',[]) or [],date.today())
        except Exception:
            return []

    def closeEvent(self,event):
        if self.runner.jobs:
            self.error('仍有请求处理中。请等待完成后关闭；关闭窗口不代表取消外部发送或模型计费。')
            event.ignore()
        else:
            if getattr(self,'tray',None):self.tray.stop()
            event.accept()


def main(argv=None):
    parser=argparse.ArgumentParser(description='IT推进管理 PySide6客户端')
    parser.add_argument('--url',default='http://127.0.0.1:18765')
    parser.add_argument('--token-file',required=True)
    args=parser.parse_args(argv)
    app=QApplication.instance() or QApplication(sys.argv[:1])
    try:client=APIClient.from_token_file(args.url,args.token_file)
    except ClientError as exc:
        QMessageBox.critical(None,'连接参数错误',str(exc));return 2
    window=MainWindow(client);window.show()
    return app.exec()


if __name__=='__main__':
    raise SystemExit(main())
