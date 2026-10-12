import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import json
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from fastapi.testclient import TestClient
from itmanager.server import create_app
from itmanager.forms import RecordDialog
from itmanager.ai_form_ui import attach_ai_assistant

class Runner:
    def submit(self,work,done,failed):
        try:done(work())
        except Exception as e:failed(str(e))
class Client:
    def __init__(self,c):self.c=c
    def post(self,path,payload):
        r=self.c.post(path,json=payload)
        assert r.status_code==200,r.text
        return r.json()

def test_basis_to_ai_preview_apply_then_manual_save(tmp_path,monkeypatch):
    qt=QApplication.instance() or QApplication([])
    app=create_app(tmp_path,'t'*32)
    with TestClient(app,headers={'Authorization':'Bearer '+'t'*32}) as c:
        provider=c.post('/objects/providers',json={'data':{'provider':'deepseek','model':'deepseek-chat','api_key_env':'DEEPSEEK_API_KEY'}}).json()
        form=RecordDialog('tasks')
        attach_ai_assistant(form,'tasks',[],[provider],c.get('/objects/prompts').json(),Runner(),Client(c)).click()
        ui=form.ai_assistant
        ui.basis.setPlainText('月底完成电脑资产盘点，形成差异报告')
        monkeypatch.setattr(ui,'preview',lambda *args:True)
        answer={'text':json.dumps({'fields':{'名称':'电脑资产盘点','下一步':'确认资产范围并核对责任人','截止日期':'2030-01-31'},'suggestions':['建立差异复核记录'],'warnings':[]},ensure_ascii=False),'usage':{},'model':'mock'}
        with patch('itmanager.ai.generate',return_value=answer):ui.submit()
        assert ui.results.rowCount()>=3
        ui.apply_selected()
        assert form.values()['名称']=='电脑资产盘点'
        assert form.values()['截止日期']=='2030-01-31'
        assert form.values()['管理建议']=='建立差异复核记录'
        assert form.is_dirty()
        assert app.state.store.list_records()==[]
        ui.reject()
        assert form.validate()
        saved=c.post('/records/tasks',json={'data':form.values()})
        assert saved.status_code==200,saved.text
        assert len(app.state.store.list_records())==1
        form.accept()
