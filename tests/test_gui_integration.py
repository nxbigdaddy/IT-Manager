"""Qt widgets + real FastAPI/SQLite, no external network or production data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication,QDialogButtonBox
from PySide6.QtCore import QTimer,QEventLoop
from fastapi.testclient import TestClient
from itmanager.server import create_app
from itmanager.gui import MainWindow,RecordDialog

TOKEN='integration-test-token-at-least-24'

class LocalClient:
    url='http://127.0.0.1:18765'
    def __init__(self,client):self.client=client
    def request(self,method,path,body=None):
        r=self.client.request(method,path,json=body)
        if r.status_code>=400:raise RuntimeError(r.text)
        return r.json()
    def get(self,path):return self.request('GET',path)
    def post(self,path,body=None):return self.request('POST',path,body)


def test_gui_form_save_refresh_and_report_with_real_service(tmp_path):
    qt=QApplication.instance() or QApplication([])
    app=create_app(tmp_path,TOKEN)
    with TestClient(app,headers={'Authorization':'Bearer '+TOKEN}) as http:
        client=LocalClient(http)
        window=MainWindow(client,autoload=False)
        errors=[];window.error=lambda text:errors.append(text)
        dialog=RecordDialog('tasks',parent=window)
        dialog.widgets['名称'][0].setText('GUI闭环验证任务')
        dialog.widgets['负责人'][0].setText('虚构IT负责人')
        # Drive the actual dialog Save button through the event loop.
        QTimer.singleShot(30,lambda:dialog.buttons.button(QDialogButtonBox.Save).click())
        watchdog=QTimer();watchdog.setSingleShot(True);watchdog.timeout.connect(dialog.accept);watchdog.start(5000)
        window.persist_dialog(dialog,lambda data:client.post('/records/tasks',{'data':data}))
        watchdog.stop()
        loop=QEventLoop()
        def wait_refresh():
            if not window.runner.jobs:loop.quit()
            else:QTimer.singleShot(10,wait_refresh)
        QTimer.singleShot(10,wait_refresh);QTimer.singleShot(5000,loop.quit);loop.exec()
        assert errors==[]
        assert any(r['data'].get('名称')=='GUI闭环验证任务' for r in window.records)
        for module in ('tasks','reviews','plans','projects','risks'):
            window.nav.setCurrentRow(window.keys.index(module));qt.processEvents()
            assert window.current==module
        draft=client.post('/drafts',{'title':'GUI任务下达','content':'根据已保存任务安排工作'})
        approved=client.post('/drafts/'+draft['id']+'/review',{'revision':1,'approved':True})
        assert approved['status']=='approved'
        window.close();window.deleteLater();qt.processEvents()
