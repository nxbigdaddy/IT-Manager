"""Evidence attachments over HTTP: hash, size, ownership and no execution."""
import base64
from pathlib import Path

from fastapi.testclient import TestClient
from itmanager.server import create_app
from itmanager.attachments import MAX_FILE_BYTES

PDF = b'%PDF-1.4 demo evidence\n'


def client_for(tmp_path):
    app = create_app(tmp_path, 'a' * 32)
    return app, TestClient(app, headers={'Authorization': 'Bearer ' + 'a' * 32})


def upload(c, record_id, name='证据.pdf', content=PDF, note='演示证据'):
    return c.post('/attachments', json={'record_id': record_id, 'name': name,
                                        'content_base64': base64.b64encode(content).decode('ascii'),
                                        'note': note})


def test_attachment_round_trip_and_verification(tmp_path):
    app, c = client_for(tmp_path)
    with c:
        record = c.post('/records/tasks', json={'data': {'名称': '需要证据的任务'}}).json()
        added = upload(c, record['id'])
        assert added.status_code == 200, added.text
        meta = added.json()
        assert meta['sha256'] and meta['size'] == len(PDF)
        assert meta['name'] == '证据.pdf'
        listing = c.get('/attachments', params={'record_id': record['id']}).json()
        assert [item['id'] for item in listing] == [meta['id']]
        content = c.get(f"/attachments/{meta['id']}/content").json()
        assert base64.b64decode(content['content_base64']) == PDF
        assert c.get(f"/attachments/{meta['id']}/verify").json()['ok'] is True
        # The file must live inside the server data directory, not an arbitrary path.
        # create_app resolves the data dir (Windows temp dirs can be junctions).
        base = Path(tmp_path).resolve() / 'attachments' / record['id']
        assert len(list(base.glob('*'))) == 1
        assert (base / meta['stored'].split('/')[-1]).is_file()
        removed = c.post('/attachments/delete', json={'id': meta['id']})
        assert removed.status_code == 200, removed.text
        assert c.get('/attachments', params={'record_id': record['id']}).json() == []
        assert c.get(f"/attachments/{meta['id']}/content").status_code == 400


def test_attachment_guards(tmp_path):
    app, c = client_for(tmp_path)
    with c:
        record = c.post('/records/tasks', json={'data': {'名称': '任务'}}).json()
        assert upload(c, 'NOT-A-RECORD').status_code == 400
        assert upload(c, record['id'], name='payload.exe', content=b'MZ').status_code == 400
        assert upload(c, record['id'], name='script.ps1', content=b'echo').status_code == 400
        assert upload(c, record['id'], name='noextension', content=b'data').status_code == 400
        assert upload(c, record['id'], content=b'').status_code == 400
        assert upload(c, record['id'], content=b'x' * (MAX_FILE_BYTES + 1)).status_code == 400
        # Path traversal in the display name must not escape the record folder.
        tricky = upload(c, record['id'], name='../../escape.pdf')
        assert tricky.status_code == 200, tricky.text
        assert '/' not in tricky.json()['name'] and '..' not in tricky.json()['name']
        for index in range(19):
            upload(c, record['id'], name=f'证据{index}.pdf')
        assert upload(c, record['id'], name='too-many.pdf').status_code == 400
        assert c.get('/attachments/NOPE/content').status_code == 400


def test_attachment_delete_requires_current_version(tmp_path):
    app, c = client_for(tmp_path)
    with c:
        record = c.post('/records/tasks', json={'data': {'名称': '任务'}}).json()
        meta = upload(c, record['id']).json()
        # Metadata is immutable once stored, so a wrong version is refused when supplied.
        assert c.post('/attachments/delete', json={'id': meta['id'], 'version': 99}).status_code == 400
        assert c.post('/attachments/delete', json={'id': 'missing'}).status_code == 400
        assert c.post('/attachments/delete', json={'id': meta['id']}).status_code == 200
        assert c.post('/attachments/delete', json={'id': meta['id']}).status_code == 400
