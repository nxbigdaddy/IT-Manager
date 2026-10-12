from pathlib import Path
from unittest.mock import patch
import pytest
from itmanager.store import Store
from itmanager.restore import restore_backup


def test_offline_restore_preserves_current_and_disables_schedules(tmp_path):
    source=Store(tmp_path/'source.sqlite3')
    row=source.save_record('tasks',{'名称':'备份中的任务'})
    source.put('schedules',{'enabled':True,'name':'旧计划'})
    backup=source.backup(tmp_path/'backups')
    target=Store(tmp_path/'target.sqlite3')
    target.save_record('tasks',{'名称':'恢复前任务'})
    with patch('itmanager.restore.socket.create_connection',side_effect=OSError):
        result=restore_backup(tmp_path/'backups'/backup,tmp_path/'target.sqlite3',confirmed=True)
    restored=Store(tmp_path/'target.sqlite3')
    assert restored.list_records()[0]['id']==row['id']
    assert restored.objects('schedules')[0]['enabled'] is False
    assert Path(result['preserved']).is_file()
    assert restored.record_history(row['id'])


def test_restore_requires_offline_and_confirmation(tmp_path):
    with pytest.raises(ValueError,match='明确确认'):restore_backup('none','none')
    with patch('itmanager.restore.socket.create_connection'):
        with pytest.raises(ValueError,match='仍在监听'):restore_backup(tmp_path/'s',tmp_path/'t',confirmed=True)
