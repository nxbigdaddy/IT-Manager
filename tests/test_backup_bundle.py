"""Complete backup bundles: hostile ZIP inspection and offline atomic restore."""
import hashlib
import json
import stat
import zipfile
from pathlib import Path

import pytest

import itmanager.backup_bundle as bundle_module
from itmanager import __version__
from itmanager.backup_bundle import BackupBundle, SAFE_BUNDLE_ERRORS
from itmanager.store import Store


def service(tmp_path, *, attachment=True, vault=True):
    root = tmp_path / 'data'
    store = Store(root / 'itmanager.sqlite3')
    store.save_record('tasks', {'名称': '包内任务'})
    if attachment:
        item = root / 'attachments' / 'TASK-1' / '证据.txt'
        item.parent.mkdir(parents=True)
        item.write_text('中文附件', encoding='utf-8')
    if vault:
        secret = root / 'secrets' / 'vault.json'
        secret.parent.mkdir(parents=True)
        secret.write_text('{"dpapi":"ciphertext"}', encoding='utf-8')
    return root, store, BackupBundle(root, store)


def created(tmp_path, **kwargs):
    root, store, bundles = service(tmp_path, **kwargs)
    result = bundles.create(tmp_path / 'exports')
    return root, store, bundles, result, Path(result['path'])


def rewrite(source, target, mutate_manifest=None, extras=(), omit=()):
    """Rewrite a generated bundle, optionally changing JSON and members."""
    with zipfile.ZipFile(source) as old:
        members = [(info.filename, old.read(info.filename), info) for info in old.infolist()]
    manifest = json.loads(next(data for name, data, _ in members if name == 'manifest.json'))
    if mutate_manifest:
        mutate_manifest(manifest)
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_STORED) as new:
        for name, data, info in members:
            if name in omit:
                continue
            if name == 'manifest.json':
                data = json.dumps(manifest, ensure_ascii=False).encode()
            new.writestr(info, data)
        for item in extras:
            if len(item) == 2:
                new.writestr(item[0], item[1])
            else:
                new.writestr(item[0], item[1], compress_type=item[2])
    return target


def error(bundle, path, expected):
    with pytest.raises(ValueError) as caught:
        bundle.inspect(path)
    assert str(caught.value) == expected
    assert str(caught.value) in SAFE_BUNDLE_ERRORS


def test_create_contains_database_attachment_and_dpapi_vault(tmp_path):
    _root, _store, bundles, result, path = created(tmp_path)
    assert result['name'].startswith('itmanager-bundle-') and result['name'].endswith('.zip')
    assert len(result['name'].split('-')[-1].removesuffix('.zip')) == 8
    assert path.is_file()
    with zipfile.ZipFile(path) as archive:
        assert set(archive.namelist()) == {'manifest.json', 'database/itmanager.sqlite3',
                                           'attachments/TASK-1/证据.txt', 'secrets/vault.json'}
    checked = bundles.inspect(path)
    assert checked['ok'] and checked['files_count'] == 3
    assert 'DPAPI' in checked['warnings'][0]


def test_create_manifest_and_return_hashes_are_exact(tmp_path):
    _root, _store, _bundle, result, path = created(tmp_path)
    assert result['size'] == path.stat().st_size
    assert result['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = result['manifest']
    assert manifest['schema_version'] == 1 and manifest['app_version'] == __version__
    assert manifest['database'] == manifest['files'][0]
    assert 'manifest.json' not in {row['path'] for row in manifest['files']}
    with zipfile.ZipFile(path) as archive:
        for row in manifest['files']:
            data = archive.read(row['path'])
            assert row['size'] == len(data)
            assert row['sha256'] == hashlib.sha256(data).hexdigest()


def test_unicode_attachment_round_trip_name(tmp_path):
    _root, _store, bundles, _result, path = created(tmp_path)
    assert 'attachments/TASK-1/证据.txt' in [x['path'] for x in bundles.inspect(path)['manifest']['files']]


def test_create_without_attachments_or_vault(tmp_path):
    _root, _store, bundles, _result, path = created(tmp_path, attachment=False, vault=False)
    inspected = bundles.inspect(path)
    assert inspected['files_count'] == 1 and inspected['warnings'] == []


def test_create_rejects_attachment_symlink(tmp_path):
    root, _store, bundles = service(tmp_path, attachment=False)
    outside = tmp_path / 'outside.txt'; outside.write_text('x')
    (root / 'attachments').mkdir()
    try:
        (root / 'attachments' / 'link.txt').symlink_to(outside)
    except OSError:
        pytest.skip('symlink unavailable')
    with pytest.raises(ValueError) as caught:
        bundles.create(tmp_path / 'out')
    assert str(caught.value) == '备份包路径不安全'


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'C:/drive', 'attachments\\..\\escape'])
def test_inspect_rejects_unsafe_paths(tmp_path, name):
    _r, _s, bundles, _x, path = created(tmp_path)
    hostile = rewrite(path, tmp_path / 'hostile.zip', extras=[(name, b'x')])
    error(bundles, hostile, '备份包路径不安全')


def test_inspect_rejects_duplicate_members(tmp_path):
    _r, _s, bundles, _x, path = created(tmp_path)
    with zipfile.ZipFile(path) as old:
        entries = [(x.filename, old.read(x.filename)) for x in old.infolist()]
    hostile = tmp_path / 'duplicate.zip'
    with zipfile.ZipFile(hostile, 'w') as archive:
        for name, data in entries:
            archive.writestr(name, data)
        archive.writestr('manifest.json', b'{}')
    error(bundles, hostile, '备份包路径不安全')


def test_inspect_rejects_symlink_external_attribute(tmp_path):
    _r, _s, bundles, _x, path = created(tmp_path)
    with zipfile.ZipFile(path) as old:
        entries = [(x, old.read(x.filename)) for x in old.infolist()]
    hostile = tmp_path / 'symlink.zip'
    with zipfile.ZipFile(hostile, 'w') as archive:
        for info, data in entries:
            if info.filename == 'attachments/TASK-1/证据.txt':
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, data)
    error(bundles, hostile, '备份包路径不安全')


@pytest.mark.parametrize('field,value', [('sha256', '0' * 64), ('size', 999)])
def test_inspect_rejects_hash_or_size_tampering(tmp_path, field, value):
    _r, _s, bundles, _x, path = created(tmp_path)
    def change(manifest):
        manifest['files'][1][field] = value
    hostile = rewrite(path, tmp_path / ('bad-' + field + '.zip'), change)
    error(bundles, hostile, '备份包校验失败')


def test_inspect_rejects_missing_manifest(tmp_path):
    _r, _s, bundles, _x, path = created(tmp_path)
    hostile = rewrite(path, tmp_path / 'missing-manifest.zip', omit={'manifest.json'})
    error(bundles, hostile, '备份包格式无效')


def test_inspect_rejects_missing_database(tmp_path):
    _r, _s, bundles, _x, path = created(tmp_path)
    def change(manifest):
        manifest['files'] = [row for row in manifest['files'] if row['kind'] != 'database']
    hostile = rewrite(path, tmp_path / 'missing-db.zip', change, omit={'database/itmanager.sqlite3'})
    error(bundles, hostile, '备份包格式无效')


def test_inspect_rejects_two_database_declarations(tmp_path):
    _r, _s, bundles, _x, path = created(tmp_path)
    with zipfile.ZipFile(path) as archive:
        db = archive.read('database/itmanager.sqlite3')
    def change(manifest):
        extra = dict(manifest['database'], path='database/second.sqlite3')
        manifest['files'].append(extra)
    hostile = rewrite(path, tmp_path / 'two-db.zip', change,
                      extras=[('database/second.sqlite3', db)])
    error(bundles, hostile, '备份包格式无效')


def test_inspect_rejects_unknown_schema(tmp_path):
    _r, _s, bundles, _x, path = created(tmp_path)
    hostile = rewrite(path, tmp_path / 'schema.zip', lambda m: m.update(schema_version=2))
    error(bundles, hostile, '备份包格式无效')


def test_member_count_limit_is_patchable(tmp_path, monkeypatch):
    _r, _s, bundles, _x, path = created(tmp_path)
    monkeypatch.setattr(bundle_module, 'MAX_MEMBERS', 1)
    error(bundles, path, '备份包成员过多或过大')


def test_single_member_limit_is_patchable(tmp_path, monkeypatch):
    _r, _s, bundles, _x, path = created(tmp_path)
    monkeypatch.setattr(bundle_module, 'MAX_MEMBER_SIZE', 4)
    error(bundles, path, '备份包成员过多或过大')


def test_total_size_limit_is_patchable(tmp_path, monkeypatch):
    _r, _s, bundles, _x, path = created(tmp_path)
    monkeypatch.setattr(bundle_module, 'MAX_TOTAL_SIZE', 10)
    error(bundles, path, '备份包成员过多或过大')


def test_compression_bomb_ratio_rejected(tmp_path):
    _r, _s, bundles, _x, path = created(tmp_path)
    hostile = rewrite(path, tmp_path / 'bomb.zip', extras=[
        ('bomb.txt', b'0' * 100000, zipfile.ZIP_DEFLATED)])
    error(bundles, hostile, '备份包成员过多或过大')


def test_invalid_database_maps_to_safe_error(tmp_path):
    _r, _s, bundles, _x, path = created(tmp_path)
    data = b'not sqlite'
    def change(manifest):
        row = next(x for x in manifest['files'] if x['kind'] == 'database')
        row.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        manifest['database'] = dict(row)
    hostile = rewrite(path, tmp_path / 'bad-db.zip', change,
                      extras=[('database/itmanager.sqlite3.replacement', b'')],
                      omit={'database/itmanager.sqlite3'})
    # Rebuild once more with the declared member, avoiding duplicate utility semantics.
    with zipfile.ZipFile(hostile) as old:
        entries = [(x.filename, old.read(x.filename)) for x in old.infolist()
                   if x.filename != 'database/itmanager.sqlite3.replacement']
    with zipfile.ZipFile(hostile, 'w') as new:
        for name, value in entries: new.writestr(name, value)
        new.writestr('database/itmanager.sqlite3', data)
    error(bundles, hostile, '备份包数据库无效')


def test_inspect_does_not_extract_attachments(tmp_path, monkeypatch):
    _r, _s, bundles, _x, path = created(tmp_path)
    writes = []
    original = Path.open
    def watched(self, mode='r', *args, **kwargs):
        if 'w' in mode and self.name == '证据.txt':
            writes.append(self)
        return original(self, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', watched)
    assert bundles.inspect(path)['ok']
    assert writes == []


def test_restore_requires_exact_confirmation(tmp_path):
    _r, _s, bundles, _x, path = created(tmp_path)
    with pytest.raises(ValueError) as caught:
        bundles.restore(path, tmp_path / 'target')
    assert str(caught.value) == '恢复需明确确认'


def test_restore_requires_service_stopped(tmp_path):
    _r, _s, bundles, _x, path = created(tmp_path)
    with pytest.raises(ValueError) as caught:
        bundles.restore(path, tmp_path / 'target', confirm=True)
    assert str(caught.value) == '恢复前必须停止服务'


def test_restore_preserves_existing_database_attachments_and_vault(tmp_path):
    _r, _s, bundles, _x, path = created(tmp_path)
    target = tmp_path / 'target'
    old = Store(target / 'itmanager.sqlite3'); old.save_record('tasks', {'名称': '旧'})
    (target / 'attachments' / 'OLD').mkdir(parents=True)
    (target / 'attachments' / 'OLD' / 'old.txt').write_text('old')
    (target / 'secrets').mkdir()
    (target / 'secrets' / 'vault.json').write_text('old vault')
    result = bundles.restore(path, target, confirm=True, service_stopped=True)
    assert len(result['preserved']) == 3
    assert list(target.glob('itmanager.sqlite3.before-restore-*.bak'))
    assert list(target.glob('attachments.before-restore-*'))
    assert list((target / 'secrets').glob('vault.json.before-restore-*.bak'))
    assert (target / 'attachments' / 'TASK-1' / '证据.txt').read_text(encoding='utf-8') == '中文附件'


def test_restore_disables_all_plans_and_records_audit(tmp_path):
    root, store, bundles = service(tmp_path)
    store.put('schedules', {'name': '计划', 'enabled': True}, 'schedule-1')
    path = Path(bundles.create(tmp_path / 'out')['path'])
    target = tmp_path / 'target'
    bundles.restore(path, target, confirm=True, service_stopped=True)
    restored = Store(target / 'itmanager.sqlite3')
    assert restored.objects('schedules')[0]['enabled'] is False
    assert any(row['action'] == '离线恢复' and '周期计划已停用' in row['detail']
               for row in restored.audit_rows())


def test_restore_failure_does_not_overwrite_target(tmp_path, monkeypatch):
    _r, _s, bundles, _x, path = created(tmp_path)
    target = tmp_path / 'target'; target.mkdir(); marker = target / 'keep.txt'; marker.write_text('keep')
    def fail(_database):
        raise ValueError('恢复失败')
    monkeypatch.setattr(bundles, '_disable_schedules', fail)
    with pytest.raises(ValueError) as caught:
        bundles.restore(path, target, confirm=True, service_stopped=True)
    assert str(caught.value) == '恢复失败'
    assert marker.read_text() == 'keep'


def test_list_bundles_includes_valid_and_bad_without_raising(tmp_path):
    _r, _s, bundles, _x, path = created(tmp_path)
    bad = path.parent / 'broken.zip'; bad.write_bytes(b'not zip')
    rows = bundles.list_bundles(path.parent)
    assert {row['name'] for row in rows} == {path.name, bad.name}
    assert next(row for row in rows if row['name'] == path.name)['ok'] is True
    broken = next(row for row in rows if row['name'] == bad.name)
    assert broken['ok'] is False and broken['error'] in SAFE_BUNDLE_ERRORS


def test_missing_bundle_safe_error(tmp_path):
    _r, _s, bundles = service(tmp_path)
    error(bundles, tmp_path / 'none.zip', '备份包不存在')


def test_every_raised_value_error_in_suite_uses_safe_constant(tmp_path):
    _r, _s, bundles = service(tmp_path)
    cases = [
        lambda: bundles.inspect(tmp_path / 'absent'),
        lambda: bundles.restore(tmp_path / 'absent', tmp_path / 'target'),
        lambda: bundles.restore(tmp_path / 'absent', tmp_path / 'target', confirm=True),
    ]
    for call in cases:
        with pytest.raises(ValueError) as caught:
            call()
        assert str(caught.value) in SAFE_BUNDLE_ERRORS
