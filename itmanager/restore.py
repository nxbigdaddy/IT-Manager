"""Explicit offline restore; refuse while local service port accepts connections."""
import argparse
from pathlib import Path
import socket
import sqlite3
from datetime import datetime
from .store import Store


def restore_backup(source, target, host='127.0.0.1',port=18765, confirmed=False):
    if not confirmed:raise ValueError('恢复需明确确认；请先关闭服务并保留当前数据')
    source=Path(source).resolve();target=Path(target).resolve()
    if source==target:raise ValueError('源备份与目标数据库不能相同')
    try:
        with socket.create_connection((host,port),timeout=1):pass
    except OSError:pass
    else:raise ValueError('服务端口仍在监听，请先停止服务；不允许在线恢复')
    if not source.is_file():raise ValueError('备份文件不存在')
    # Use read-only integrity check before touching the target.
    uri=source.as_uri()+'?mode=ro'
    with sqlite3.connect(uri,uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('备份完整性检查失败')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'records','objects','audit','deliveries','schema_versions'}<=tables:raise ValueError('不是兼容的IT管理数据库')
    target.parent.mkdir(parents=True,exist_ok=True)
    preserved=None
    if target.exists():
        preserved=target.with_name(target.name+'.before-restore-'+datetime.now().strftime('%Y%m%d%H%M%S')+'.bak')
        with sqlite3.connect(target) as old,sqlite3.connect(preserved) as save:old.backup(save)
    with sqlite3.connect(uri,uri=True) as src,sqlite3.connect(target) as dst:src.backup(dst)
    restored=Store(target)
    for job in restored.objects('schedules'):
        job['enabled']=False
        restored.put('schedules',job,job['id'])
    restored.record_audit('离线恢复','database','所有周期计划已停用，请核对发送记录后重新启用')
    return {'restored':str(target),'preserved':str(preserved) if preserved else None,'schedules_disabled':True}


def main():
    parser=argparse.ArgumentParser(description='Offline SQLite restore')
    parser.add_argument('backup');parser.add_argument('--target',default='data/itmanager.sqlite3');parser.add_argument('--confirm',action='store_true')
    args=parser.parse_args();print(restore_backup(args.backup,args.target,confirmed=args.confirm))

if __name__=='__main__':main()
