"""Local source launcher. No compilation, no secret command-line arguments."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.request
import urllib.error

ROOT=Path(__file__).resolve().parent.parent
DATA=ROOT/'data'


def initialize():
    DATA.mkdir(exist_ok=True)
    token_file=DATA/'admin.token'
    if os.name=='nt':
        # Restrict the directory before writing new secrets; fail closed on ACL error.
        user=os.environ.get('USERDOMAIN','')+'\\'+os.environ.get('USERNAME','')
        if not os.environ.get('USERNAME'):raise SystemExit('Cannot determine Windows account for data ACL.')
        result=subprocess.run(['icacls',str(DATA),'/inheritance:r','/grant:r',user+':(OI)(CI)F','*S-1-5-18:(OI)(CI)F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if result.returncode:raise SystemExit('Cannot secure data directory ACL; startup stopped.')
    else:DATA.chmod(0o700)
    if not token_file.exists():
        token_file.write_text(secrets.token_urlsafe(36),encoding='utf-8')
    if os.name=='nt':
        result=subprocess.run(['icacls',str(token_file),'/inheritance:r','/grant:r',user+':F','*S-1-5-18:F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if result.returncode:raise SystemExit('Cannot secure token ACL; startup stopped.')
    else:token_file.chmod(0o600)
    return token_file,token_file.read_text(encoding='utf-8').strip()


def health_info(url,token):
    req=urllib.request.Request(url+'/health',headers={'Authorization':'Bearer '+token})
    try:
        with urllib.request.urlopen(req,timeout=2) as response:return json.load(response)
    except (OSError,ValueError):return None


def health(url,token):
    value=health_info(url,token)
    return bool(value and value.get('status')=='ok')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['local','server','desktop','check']);parser.add_argument('--url',default='http://127.0.0.1:18765');parser.add_argument('--token-file')
    args=parser.parse_args()
    os.chdir(ROOT)
    if args.token_file:
        token_file=Path(args.token_file).resolve();token=token_file.read_text(encoding='utf-8').strip()
    else:token_file,token=initialize()
    os.environ['ITM_ADMIN_TOKEN']=token
    if args.mode=='check':
        import PySide6,fastapi,uvicorn,openpyxl
        print('Dependencies OK; PySide6 '+PySide6.__version__)
        return
    if args.mode=='server':
        from .server import main as server_main
        sys.argv=[sys.argv[0],'--data-dir',str(DATA),'--scheduler'];server_main();return
    if args.mode=='local' and not health(args.url,token):
        if args.url!='http://127.0.0.1:18765':raise SystemExit('local mode only supports the default loopback address')
        cmd=[sys.executable,'-m','itmanager.launcher','server','--token-file',str(token_file)]
        flags=subprocess.CREATE_NEW_CONSOLE if os.name=='nt' else 0
        process=subprocess.Popen(cmd,cwd=ROOT,env=os.environ.copy(),creationflags=flags)
        for _ in range(60):
            if health(args.url,token):break
            if process.poll() is not None:raise SystemExit('Server failed to start; inspect server console (port occupied or configuration invalid).')
            time.sleep(.25)
        else:raise SystemExit('Server health timeout; inspect server console.')
    info=health_info(args.url,token)
    if not info or info.get('status')!='ok':raise SystemExit('Service unavailable or token invalid. Start server first and verify connection settings.')
    from . import __version__
    if str(info.get('version'))!=__version__:
        raise SystemExit(f"Version mismatch: desktop {__version__}, server {info.get('version')}. Close the old server console and run start_local.bat again.")
    from .gui import main as gui_main
    sys.argv=[sys.argv[0],'--url',args.url,'--token-file',str(token_file)]
    gui_main()

if __name__=='__main__':main()
