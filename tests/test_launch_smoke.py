"""Real subprocess startup smoke on an isolated port and temporary database."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import json


def test_real_server_and_gui_start(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    token='isolated-startup-test-token-123456789'
    token_file=tmp_path/'admin.token';token_file.write_text(token)
    env=os.environ.copy();env['ITM_ADMIN_TOKEN']=token;env['QT_QPA_PLATFORM']='offscreen'
    log=open(tmp_path/'server.log','w',encoding='utf-8')
    server=subprocess.Popen([sys.executable,'-m','itmanager.server','--port',str(port),'--data-dir',str(tmp_path/'data'),'--scheduler'],env=env,stdout=log,stderr=log)
    gui=None
    try:
        ready=False
        for _ in range(80):
            if server.poll() is not None:break
            try:
                req=urllib.request.Request(f'http://127.0.0.1:{port}/health',headers={'Authorization':'Bearer '+token})
                with urllib.request.urlopen(req,timeout=.5) as response:
                    ready=json.load(response)['status']=='ok'
                if ready:break
            except OSError:time.sleep(.1)
        assert ready,'server startup failed'
        command=[sys.executable,'-m','itmanager.gui','--url',f'http://127.0.0.1:{port}','--token-file',str(token_file)]
        if os.name=='nt':command=['cmd.exe','/c','start_desktop.bat','--url',f'http://127.0.0.1:{port}','--token-file',str(token_file)]
        gui=subprocess.Popen(command,env=env,stdout=log,stderr=log)
        time.sleep(2)
        assert gui.poll() is None,'GUI failed to remain running'
    finally:
        if gui and gui.poll() is None:
            if os.name=='nt':subprocess.run(['taskkill','/PID',str(gui.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            else:gui.terminate()
            gui.wait(timeout=10)
        if server.poll() is None:server.terminate();server.wait(timeout=10)
        log.close()
