"""Scheduled consistency backups: plan validation, one run, no automatic deletion."""
import base64
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from itmanager.server import create_app


def client_for(tmp_path):
    app = create_app(tmp_path, 'k' * 32)
    return app, TestClient(app, headers={'Authorization': 'Bearer ' + 'k' * 32})


def test_backup_schedule_saves_and_rejects_bad_plans(tmp_path):
    app, c = client_for(tmp_path)
    with c:
        good = c.post('/objects/schedules', json={'data': {
            'name': '每日一致性备份', 'kind': 'backup', 'mode': 'draft', 'owner': '',
            'period_days': 7, 'keep': 7, 'scope': '全部业务库',
            'schedule': {'frequency': 'daily', 'timezone': 'Asia/Shanghai', 'hour': 2, 'minute': 30},
            'enabled': True}})
        assert good.status_code == 200, good.text
        job = good.json()
        assert job['next_run']
        assert datetime.fromisoformat(job['next_run']) > datetime.now(timezone.utc) - timedelta(minutes=1)
        assert job['mode'] == 'draft'  # a backup never sends messages
        bad = c.post('/objects/schedules', json={'data': {
            'name': '坏计划', 'kind': 'backup', 'mode': 'draft', 'period_days': 7, 'keep': 7,
            'schedule': {'frequency': 'weekly', 'timezone': 'Not/AZone', 'hour': 2, 'minute': 0},
            'enabled': True}})
        assert bad.status_code == 400
        assert bad.json()['user_message']


def test_due_backup_schedule_creates_one_file_and_records_health(tmp_path):
    app, c = client_for(tmp_path)
    store = app.state.store
    with c:
        store.put('schedules', {'name': '到期备份', 'kind': 'backup', 'mode': 'draft', 'keep': 2,
                                'scope': '全部业务库', 'enabled': True, 'period_days': 7,
                                'schedule': {'frequency': 'daily', 'timezone': 'Asia/Shanghai',
                                             'hour': 2, 'minute': 0},
                                'next_run': (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()},
                  'sched-due-1')
        app.state.tick()
        backups = list((tmp_path / 'backups').glob('*.sqlite3'))
        assert len(backups) == 1, backups
        runs = store.objects('backup_runs')
        assert len(runs) == 1
        assert runs[0]['file'] == backups[0].name
        assert runs[0]['ok'] in (True, False)
        # Running the tick again must not create a second copy for the same slot.
        app.state.tick()
        assert len(list((tmp_path / 'backups').glob('*.sqlite3'))) == 1
        schedules = {job['id']: job for job in store.objects('schedules')}
        assert datetime.fromisoformat(schedules['sched-due-1']['next_run']) > datetime.now(timezone.utc)


def test_backup_never_deletes_files_even_when_retention_suggests(tmp_path):
    app, c = client_for(tmp_path)
    store = app.state.store
    folder = tmp_path / 'backups'
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(4):
        (folder / f'old-{index}.sqlite3').write_bytes(b'x' * 16)
    with c:
        store.put('schedules', {'name': '保留策略', 'kind': 'backup', 'mode': 'draft', 'keep': 1,
                                'scope': '全部业务库', 'enabled': True, 'period_days': 7,
                                'schedule': {'frequency': 'daily', 'timezone': 'UTC', 'hour': 0, 'minute': 0},
                                'next_run': (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()},
                  'sched-keep-1')
        app.state.tick()
        remaining = sorted(path.name for path in folder.glob('*.sqlite3'))
        assert len(remaining) == 5, remaining  # 4 pre-existing + the new one, none deleted
        runs = store.objects('backup_runs')
        assert runs[0]['suggested_delete'], 'retention must be reported as a suggestion only'
        assert all(name in remaining for name in runs[0]['suggested_delete'])
