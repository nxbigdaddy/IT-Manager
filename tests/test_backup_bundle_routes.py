"""Complete backup-bundle HTTP routes: database + attachments + optional DPAPI vault."""
from pathlib import Path

from fastapi.testclient import TestClient
from itmanager import __version__
from itmanager.server import create_app

TOKEN = 'bundle-route-token-at-least-24'


def test_create_list_and_inspect_bundle(tmp_path):
    app = create_app(tmp_path, TOKEN)
    store = app.state.store
    with TestClient(app, headers={'Authorization': 'Bearer ' + TOKEN}) as c:
        record = c.post('/records/tasks', json={'data': {'名称': '演示任务'}}).json()
        # A small opaque attachment proves the bundle is more than a database snapshot.
        import base64
        uploaded = c.post('/attachments', json={
            'record_id': record['id'], 'name': '证据.txt',
            'content_base64': base64.b64encode(b'evidence').decode('ascii'), 'note': '演示'}).json()
        created = c.post('/backup/bundle')
        assert created.status_code == 200, created.text
        bundle = created.json()
        assert bundle['manifest']['app_version'] == __version__
        kinds = {item['kind'] for item in bundle['manifest']['files']}
        assert {'database', 'attachment'} <= kinds
        path = Path(bundle['path'])
        assert path.is_file() and path.parent == (tmp_path / 'backups').resolve()
        listing = c.get('/backup/bundles').json()
        assert any(row['name'] == bundle['name'] and row['ok'] for row in listing)
        inspected = c.post('/backup/bundle/inspect', json={'name': bundle['name']})
        assert inspected.status_code == 200, inspected.text
        assert inspected.json()['ok'] is True
        assert inspected.json()['files_count'] >= 2
        assert uploaded['stored'] in {item['path'] for item in inspected.json()['manifest']['files']}


def test_bundle_inspect_rejects_paths_outside_backup_folder(tmp_path):
    app = create_app(tmp_path, TOKEN)
    with TestClient(app, headers={'Authorization': 'Bearer ' + TOKEN}) as c:
        assert c.post('/backup/bundle/inspect', json={'name': '../evil.zip'}).status_code == 400
        assert c.post('/backup/bundle/inspect', json={'name': 'C:\\evil.zip'}).status_code == 400
        assert c.post('/backup/bundle/inspect', json={'name': 'missing.zip'}).status_code == 400
