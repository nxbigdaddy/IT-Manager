"""Workflow dialogs: explicit task actions and confirmed action creation."""
import json
import uuid
from PySide6.QtWidgets import QDialog,QVBoxLayout,QLabel,QTableWidget,QTableWidgetItem,QPushButton,QHBoxLayout,QHeaderView,QMessageBox
from .forms import FormDialog
from .schema import MODULES

ACTIONS={'progress':'记录进展','block':'标记阻塞','submit':'提交验收','complete':'确认完成','postpone':'延期留痕'}

class ManagementMixin:
    def task_action_dialog(self,record):
        if not record or record.get('module')!='tasks':return
        if record['data'].get('状态') in ('已完成','已取消','已关闭'):
            self.statusBar().showMessage('此任务已结束；请查看历史或创建后续任务。');return
        from PySide6.QtWidgets import QInputDialog
        label,ok=QInputDialog.getItem(self,'推进任务','选择本次操作',list(ACTIONS.values()),0,False)
        if not ok:return
        action=next(k for k,v in ACTIONS.items() if v==label)
        fields={'progress':[('content','本次进展 *','long',''),('next_step','下一步','long',record['data'].get('下一步',''))],
                'block':[('reason','阻塞原因 *','long',''),('next_step','解决动作 *','long','')],
                'submit':[('evidence','交付证据 *','long',record['data'].get('完成证据','')),('acceptor','验收人 *','text',record['data'].get('验收人',''))],
                'complete':[('evidence','完成证据 *','long',record['data'].get('完成证据','')),('acceptor','验收人 *','text',record['data'].get('验收人',''))],
                'postpone':[('due_date','新截止日期 *','date',record['data'].get('截止日期','')),('reason','延期原因 *','long',''),('approver','批准人 *','text','')]}[action]
        dialog=FormDialog(label+' · '+record['data'].get('名称',''),fields,parent=self)
        client=self.client;request={'fingerprint':None,'id':None}
        def save(data):
            fingerprint=json.dumps(data,ensure_ascii=False,sort_keys=True)
            if fingerprint!=request['fingerprint']:request.update(fingerprint=fingerprint,id=uuid.uuid4().hex)
            return client.post('/tasks/'+record['id']+'/action',{'version':record['version'],'action':action,'payload':data,'request_id':request['id']})
        self.persist_dialog(dialog,save)

    def related_records_dialog(self,record):
        if not record:return
        oid=record['id'];name=record['data'].get('姓名') if record['module']=='people' else None
        candidates=[]
        for row in self.records:
            if row['id']==oid:continue
            data=row['data']
            linked=any(oid in str(value) for value in data.values())
            if name:linked=linked or any(data.get(key)==name for key in ('负责人','项目负责人','业务负责人','技术负责人','责任人','验收人','复核人','主管','执行人'))
            if linked:candidates.append(row)
        dialog=QDialog(self);dialog.setWindowTitle('关联工作 · '+next(iter(record['data'].values()),''));dialog.resize(900,560)
        layout=QVBoxLayout(dialog);layout.addWidget(QLabel('按关联ID和责任人姓名辅助检索；同名人员请核对，结果不是权限边界。'))
        table=QTableWidget(len(candidates),4);table.setHorizontalHeaderLabels(['模块','事项','状态','截止日期']);table.setEditTriggers(QTableWidget.NoEditTriggers);table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for i,row in enumerate(candidates):
            for j,text in enumerate([MODULES[row['module']][0],row['data'].get(MODULES[row['module']][1][0],''),row['data'].get('状态',row['data'].get('结论','')),row['data'].get('截止日期','')]):table.setItem(i,j,QTableWidgetItem(text))
        layout.addWidget(table)
        if not candidates:layout.addWidget(QLabel('暂无关联记录。创建任务或日志时选择关联项目/人员后即可在此查看。'))
        table.doubleClicked.connect(lambda index:self.edit_record(candidates[index.row()]['module'],candidates[index.row()]))
        close=QPushButton('关闭');close.clicked.connect(dialog.accept);layout.addWidget(close);dialog.exec()

    def create_action_dialog(self,source_id,suggestions):
        dialog=ActionCreationDialog(source_id,suggestions,self.client,self.runner,self)
        dialog.exec()
        self.refresh()

class ActionCreationDialog(QDialog):
    def __init__(self,source_id,suggestions,client,runner,parent=None):
        super().__init__(parent);self.source_id=source_id;self.client=client;self.runner=runner;self.busy=False;self.request_id=uuid.uuid4().hex;self.fingerprint=None
        self.setWindowTitle('确认建议并建立任务');self.resize(1050,620)
        layout=QVBoxLayout(self);layout.addWidget(QLabel('请逐项编辑并选择任务。仅创建待执行任务，不发送通知、不视为审批；最多20项。删除不需要的文字，补充负责人和期限。'))
        from PySide6.QtCore import Qt
        self.table=QTableWidget(min(len(suggestions),20),6);self.table.setHorizontalHeaderLabels(['选择 / 任务名称','负责人','截止日期 YYYY-MM-DD','下一步','验收标准','来源'])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for i,s in enumerate(suggestions[:20]):
            item=QTableWidgetItem(str(s)[:500]);item.setCheckState(Qt.Unchecked);self.table.setItem(i,0,item)
            for j in range(1,5):self.table.setItem(i,j,QTableWidgetItem(''))
            source=QTableWidgetItem(source_id);source.setFlags(Qt.ItemIsEnabled);self.table.setItem(i,5,source)
        layout.addWidget(self.table)
        self.error=QLabel();self.error.setWordWrap(True);layout.addWidget(self.error)
        buttons=QHBoxLayout();self.submit_button=QPushButton('确认创建选中任务');self.submit_button.clicked.connect(self.submit);buttons.addWidget(self.submit_button)
        close=QPushButton('关闭');close.clicked.connect(self.reject);buttons.addWidget(close);layout.addLayout(buttons)
    def submit(self):
        from PySide6.QtCore import Qt
        if self.busy:return
        items=[]
        for i in range(self.table.rowCount()):
            if self.table.item(i,0).checkState()==Qt.Checked:
                values=[self.table.item(i,j).text().strip() for j in range(5)]
                if not values[0]:self.error.setText('选中任务必须填写名称。');return
                items.append(dict(zip(('name','owner','due_date','next_step','acceptance'),values)))
        if not items:self.error.setText('请至少勾选一个任务。');return
        if QMessageBox.question(self,'确认建立','将建立 '+str(len(items))+' 项待执行任务，是否继续？',QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes:return
        fingerprint=json.dumps(items,sort_keys=True,ensure_ascii=False)
        if self.fingerprint is not None and fingerprint!=self.fingerprint:self.request_id=uuid.uuid4().hex
        self.fingerprint=fingerprint;self.busy=True;self.table.setEnabled(False);self.submit_button.setEnabled(False)
        def done(result):
            self.busy=False;self.accept()
        def failed(message):
            self.busy=False;self.table.setEnabled(True);self.submit_button.setEnabled(True);self.error.setText(message+'；输入和请求编号已保留，重试不会重复建立同一批任务。')
        self.runner.submit(lambda:self.client.post('/actions/create',{'source_id':self.source_id,'items':items,'request_id':self.request_id}),done,failed)
    def reject(self):
        if self.busy:self.error.setText('正在创建，请等待完成后关闭。');return
        super().reject()
