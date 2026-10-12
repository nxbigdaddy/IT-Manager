"""Capture actual PySide6 widgets, with fictional data and no network access."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from datetime import date,timedelta
from PySide6.QtWidgets import QApplication,QDialog
from itmanager.gui import MainWindow,CONFIG_FIELDS
from itmanager.forms import RecordDialog,FormDialog
from itmanager.schema import MODULES
from itmanager.ai_forms import default_form_prompts
from itmanager.ai_form_ui import attach_ai_assistant
from itmanager.workspaces import builtin_templates
from itmanager.reminders import default_rules
from itmanager.checklist_templates import default_templates

OUT=Path(__file__).parent/'images'
class DemoClient:
    url='http://127.0.0.1:18765'
    def get(self,path):
        if path=='/workspace-templates':return [{'id':f'demo-template-{i}','version':1,'config':t} for i,t in enumerate(builtin_templates())]
        if path=='/workspaces':return [{'id':'demo-ws','name':'IPO IT资料准备（演示）','owner':'陈经理（虚构）','start_date':'2026-10-01','due_date':'2026-12-31','template_snapshot':builtin_templates()[0]}]
        if path=='/reminders':return [dict(rule,id=f'demo-rule-{i}',version=1) for i,rule in enumerate(default_rules())]
        if path=='/checklist-templates':return [dict(t,id=f'demo-checklist-{i}',version=1) for i,t in enumerate(default_templates())]
        return []
    def post(self,*args):raise RuntimeError('截图模式禁止提交和外部调用')
class Runner:
    jobs={}
    def submit(self,work,done=None,failed=None):
        try:
            result=work()
            if done:done(result)
        except Exception as e:
            if failed:failed(str(e))
            else:raise

def main():
    OUT.mkdir(exist_ok=True)
    app=QApplication.instance() or QApplication([])
    w=MainWindow(DemoClient(),autoload=False);w.runner=Runner()
    records=[]
    for i,(module,(title,fields)) in enumerate(MODULES.items()):
        data={fields[0]:title+'样例（演示）'}
        for field in fields[1:]:
            if field.endswith('ID'):continue
            if field=='状态':data[field]='目标设定' if module=='reviews' else '进行中'
            elif field.endswith('日期'):data[field]=(date.today()+timedelta(days=5)).isoformat()
            elif field in ('负责人','责任人','项目负责人','技术负责人','业务负责人'):data[field]='陈经理（虚构）'
            elif field in ('下一步','管理建议'):data[field]='核对资料来源，确认负责人和验收标准，按周复核进展。'
            elif '证据' in field:data[field]='演示证据索引；不是实际业务凭证'
        records.append({'id':'DEMO-'+module,'module':module,'version':1,'data':data,'updated':'2026-10-10T09:00:00Z','archived':0})
    w.records=records
    w.config['providers']=[{'id':'demo-provider','name':'DeepSeek（演示配置）','provider':'deepseek','model':'deepseek-chat','api_key_env':'DEEPSEEK_API_KEY','enabled':True}]
    w.config['prompts']=[dict(p,id='demo-prompt-'+p['module'],is_default=True) for p in default_form_prompts()]
    w.config['channels']=[{'id':'demo-channel','name':'IT工作群（演示）','type':'wecom','webhook_env':'ITM_SECRET_DEMO','enabled':False}]
    w.connection.setText('演示截图 · 全部资料虚构 · 无网络调用')
    w.resize(1440,900);w.show();app.processEvents()
    def capture(widget,name):
        widget.show();app.processEvents()
        if not widget.grab().save(str(OUT/(name+'.png'))):raise RuntimeError(name)
    for key in w.keys:
        w.nav.setCurrentRow(w.keys.index(key));app.processEvents();capture(w,'page-'+key)
    # Dark theme and a larger font scale, for the preference chapter.
    w.prefs.theme='dark';w.prefs.font_scale=1.15;w.apply_theme()
    w.nav.setCurrentRow(0);app.processEvents();capture(w,'page-dashboard-dark')
    w.nav.setCurrentRow(w.keys.index('tasks'));app.processEvents();capture(w,'page-tasks-dark')
    w.prefs.theme='light';w.prefs.font_scale=1.0;w.apply_theme()
    w.nav.setCurrentRow(0);app.processEvents()
    for module in MODULES:
        dialog=RecordDialog(module,records=records,parent=w)
        attach_ai_assistant(dialog,module,records,w.config['providers'],w.config['prompts'],w.runner,w.client)
        dialog.resize(1050,760);capture(dialog,'form-'+module)
        if dialog.tabs.count()>1:
            dialog.tabs.setCurrentIndex(1);capture(dialog,'form-'+module+'-details')
        dialog.accept()
    for kind,fields in CONFIG_FIELDS.items():
        choices={'module':[(title,key) for key,(title,_) in MODULES.items()]} if kind=='prompts' else {}
        dialog=FormDialog('配置 · '+kind,fields,choices=choices,parent=w);dialog.resize(1050,760)
        capture(dialog,'config-'+kind);dialog.accept()
    task=RecordDialog('tasks',records=records,parent=w)
    attach_ai_assistant(task,'tasks',records,w.config['providers'],w.config['prompts'],w.runner,w.client).click()
    assistant=task.ai_assistant;assistant.basis.setPlainText('下月底完成公司电脑资产盘点，形成差异清单与整改计划。')
    capture(assistant,'ai-assistant');assistant.reject();task.accept()
    from unittest.mock import patch
    from itmanager.attachments_ui import AttachmentsDialog
    from itmanager.lifecycle_ui import LifecycleDialog
    attachments=AttachmentsDialog(records[0],w.client,w.runner,w)
    attachments.items=[{'id':'DEMO-ATT-1','name':'备份恢复演练记录.pdf','size':184320,'note':'演示证据',
                        'created':'2026-10-10T08:30:00+00:00','sha256':'a1b2c3d4e5f6'+'0'*52}]
    attachments.table.setRowCount(1)
    from PySide6.QtWidgets import QTableWidgetItem as _Item
    for column,text in enumerate(['备份恢复演练记录.pdf','180.0 KB','演示证据','2026-10-10 08:30:00','a1b2c3d4e5f6']):
        attachments.table.setItem(0,column,_Item(text))
    capture(attachments,'attachments-dialog');attachments.reject()
    chain_demo=LifecycleDialog(records,'TASKS-demo' if False else 'DEMO-tasks',w)
    capture(chain_demo,'lifecycle-dialog');chain_demo.reject()
    w.nav.setCurrentRow(w.keys.index('workspaces'));app.processEvents()
    w.ws_table.selectRow(0)
    with patch.object(QDialog,'exec',lambda dialog:(capture(dialog,'workspace-detail'),QDialog.Accepted)[1]):w._ws_open()
    with patch.object(QDialog,'exec',lambda dialog:(capture(dialog,'workspace-create'),QDialog.Accepted)[1]):w._ws_create()
    from itmanager.management_ui import ActionCreationDialog
    action=ActionCreationDialog('DEMO-tasks',['核实系统恢复目标并确认演练计划','完成关键账号权限复核'],w.client,w.runner,w)
    capture(action,'action-create');action.reject()
    with patch.object(w,'refresh',lambda:None),patch.object(QDialog,'exec',lambda dialog:(capture(dialog,'draft-editor'),QDialog.Accepted)[1]):
        w.draft_editor({'id':'DEMO-DRAFT','title':'IT周报（演示草稿）','content':'本周进展：已完成演示资料梳理。\n待确认：责任人、验收依据。\n风险与下一步：安排恢复演练并核查权限。','status':'draft','revision':1})
    print('Captured',len(list(OUT.glob('*.png'))),'actual widget screenshots (fictional data).',flush=True)
    # No event loop is entered; discard demo widgets without unsaved-close prompts.
    w.deleteLater()
if __name__=='__main__':main()
