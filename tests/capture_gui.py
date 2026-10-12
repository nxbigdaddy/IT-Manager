"""Generate reproducible GUI screenshots with fictional records only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
from PySide6.QtWidgets import QApplication
from itmanager.gui import MainWindow

class Mock:
    url='http://127.0.0.1:18765'
    def get(self,path):return []

app=QApplication([])
w=MainWindow(Mock(),autoload=False)
w.records=[{'id':'TASK-DEMO-001','module':'tasks','version':1,'updated':'2026-10-10T09:00:00Z','data':{'名称':'隔离环境恢复演练（演示）','负责人':'IT负责人（虚构）','优先级':'P1','截止日期':'2026-10-20','状态':'进行中','下一步':'确认测试环境与验收人'}},{'id':'RISK-DEMO-001','module':'risks','version':1,'updated':'2026-10-10T09:00:00Z','data':{'名称':'关键恢复能力待验证（演示）','等级':'高','状态':'进行中','负责人':'系统负责人（虚构）'}}]
w.connection.setText('演示截图 · 虚构数据 · 本地SQLite / PySide6')
w.show();app.processEvents()
out=Path('docs/screenshots');out.mkdir(parents=True,exist_ok=True)
w.nav.setCurrentRow(w.keys.index('tasks'));app.processEvents();w.grab().save(str(out/'tasks.png'))
w.nav.setCurrentRow(0);app.processEvents();w.grab().save(str(out/'dashboard.png'))
w.close()
