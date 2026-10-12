"""Portable, validated backup bundles for offline service recovery.

The bundle contains a SQLite consistency snapshot plus opaque attachments and,
when present, the DPAPI-encrypted vault.  Inspection never extracts attachments.
Restore is deliberately offline-only and uses a sibling staging directory before
replacing the target data directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

from . import __version__


SAFE_BUNDLE_ERRORS = frozenset({
    '备份包不存在',
    '备份包格式无效',
    '备份包路径不安全',
    '备份包成员过多或过大',
    '备份包校验失败',
    '备份包数据库无效',
    '恢复需明确确认',
    '恢复前必须停止服务',
    '目标目录无效',
    '备份包写入失败',
    '恢复失败',
})

MAX_MEMBERS = 5000
MAX_TOTAL_SIZE = 2 * 1024 * 1024 * 1024
MAX_MEMBER_SIZE = 100 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
_CHUNK = 1024 * 1024
_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_DRIVE = re.compile(r'^[A-Za-z]:')

__all__ = ['BackupBundle', 'SAFE_BUNDLE_ERRORS']


def _fail(message):
    raise ValueError(message)


def _hash_file(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open('rb') as source:
        while True:
            block = source.read(_CHUNK)
            if not block:
                break
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def _safe_member(name):
    """Return a normalized POSIX member name or reject it."""
    if not isinstance(name, str) or not name or '\x00' in name or '\\' in name:
        _fail('备份包路径不安全')
    path = PurePosixPath(name)
    if path.is_absolute() or _DRIVE.match(name) or any(part in ('', '.', '..') for part in path.parts):
        _fail('备份包路径不安全')
    normalized = path.as_posix()
    if normalized != name or name.endswith('/'):
        _fail('备份包路径不安全')
    return normalized


def _is_zip_symlink(info):
    return stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)


class BackupBundle:
    """Create, inspect and offline-restore complete service-data bundles."""

    def __init__(self, root, store):
        try:
            self.root = Path(root)
        except (TypeError, ValueError):
            _fail('目标目录无效')
        self.store = store

    def create(self, target_dir=None):
        """Create one atomic ZIP bundle and return its metadata."""
        output = Path(target_dir) if target_dir is not None else self.root / 'backups'
        temporary = None
        try:
            root = self.root.resolve(strict=False)
            output = output.resolve(strict=False)
            output.mkdir(parents=True, exist_ok=True)
            backup_dir = (root / 'backups').resolve(strict=False)
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_result = self.store.backup(backup_dir)
            database = Path(backup_result)
            if not database.is_absolute():
                database = backup_dir / database
            database = database.resolve(strict=True)
            database.relative_to(backup_dir)
            if database.is_symlink() or not database.is_file():
                _fail('备份包写入失败')

            sources = [('database/itmanager.sqlite3', database, 'database')]
            attachments = root / 'attachments'
            if attachments.exists():
                if attachments.is_symlink() or not attachments.is_dir():
                    _fail('备份包路径不安全')
                for current, dirs, names in os.walk(attachments, followlinks=False):
                    current_path = Path(current)
                    if any((current_path / name).is_symlink() for name in dirs):
                        _fail('备份包路径不安全')
                    for name in names:
                        source = current_path / name
                        if source.is_symlink():
                            _fail('备份包路径不安全')
                        if not source.is_file():
                            continue
                        relative = source.relative_to(attachments).as_posix()
                        member = _safe_member('attachments/' + relative)
                        sources.append((member, source, 'attachment'))
            vault = root / 'secrets' / 'vault.json'
            if vault.exists():
                if vault.is_symlink() or not vault.is_file():
                    _fail('备份包路径不安全')
                sources.append(('secrets/vault.json', vault, 'vault'))

            files = []
            for member, source, kind in sources:
                size, digest = _hash_file(source)
                files.append({'path': member, 'size': size, 'sha256': digest, 'kind': kind})
            database_info = dict(files[0])
            manifest = {
                'schema_version': 1,
                'created': datetime.now(timezone.utc).isoformat(),
                'app_version': __version__,
                'database': database_info,
                'files': files,
            }
            name = ('itmanager-bundle-' + datetime.now().strftime('%Y%m%d-%H%M%S-')
                    + uuid4().hex[:8] + '.zip')
            final = output / name
            fd, temporary_name = tempfile.mkstemp(prefix='.' + name + '.', suffix='.tmp', dir=output)
            os.close(fd)
            temporary = Path(temporary_name)
            # Store without compression: bundles often contain already-compressed evidence,
            # and this guarantees our own highly repetitive files never resemble a bomb.
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED,
                                 allowZip64=True) as archive:
                for member, source, _kind in sources:
                    archive.write(source, member)
                archive.writestr('manifest.json', json.dumps(
                    manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')
                ).encode('utf-8'))
            os.replace(temporary, final)
            temporary = None
            size, digest = _hash_file(final)
            return {'name': name, 'path': str(final), 'size': size,
                    'sha256': digest, 'manifest': manifest}
        except ValueError as exc:
            if str(exc) in SAFE_BUNDLE_ERRORS:
                raise
            _fail('备份包写入失败')
        except Exception:
            _fail('备份包写入失败')
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _validate_manifest(manifest, member_names):
        required_keys = {'schema_version', 'created', 'app_version', 'database', 'files'}
        if not isinstance(manifest, dict) or set(manifest) != required_keys or manifest.get('schema_version') != 1:
            _fail('备份包格式无效')
        if not isinstance(manifest.get('created'), str) or not isinstance(manifest.get('app_version'), str):
            _fail('备份包格式无效')
        try:
            created = datetime.fromisoformat(manifest['created'].replace('Z', '+00:00'))
            if created.utcoffset() is None:
                raise ValueError
        except Exception:
            _fail('备份包格式无效')
        files = manifest.get('files')
        database = manifest.get('database')
        if not isinstance(files, list) or not isinstance(database, dict):
            _fail('备份包格式无效')
        seen = set()
        database_rows = []
        for row in files:
            if not isinstance(row, dict) or set(row) != {'path', 'size', 'sha256', 'kind'}:
                _fail('备份包格式无效')
            path = _safe_member(row.get('path'))
            if path == 'manifest.json' or path in seen:
                _fail('备份包格式无效')
            seen.add(path)
            if type(row.get('size')) is not int or row['size'] < 0 or not _SHA256.fullmatch(row.get('sha256', '')):
                _fail('备份包格式无效')
            kind = row.get('kind')
            valid_kind_path = ((kind == 'database' and path == 'database/itmanager.sqlite3') or
                               (kind == 'attachment' and path.startswith('attachments/') and len(path) > 12) or
                               (kind == 'vault' and path == 'secrets/vault.json'))
            if not valid_kind_path:
                _fail('备份包格式无效')
            if kind == 'database':
                database_rows.append(row)
        if len(database_rows) != 1 or database != database_rows[0]:
            _fail('备份包格式无效')
        if seen | {'manifest.json'} != set(member_names):
            _fail('备份包格式无效')
        return files, database_rows[0]

    def inspect(self, path):
        """Validate a bundle without extracting any attachment."""
        try:
            source = Path(path)
        except (TypeError, ValueError, OSError):
            _fail('备份包不存在')
        if not source.is_file():
            _fail('备份包不存在')
        database_temp = None
        try:
            with zipfile.ZipFile(source, 'r') as archive:
                infos = archive.infolist()
                if len(infos) > MAX_MEMBERS:
                    _fail('备份包成员过多或过大')
                names = []
                total = 0
                for info in infos:
                    name = _safe_member(info.filename)
                    if name in names:
                        _fail('备份包路径不安全')
                    names.append(name)
                    if _is_zip_symlink(info):
                        _fail('备份包路径不安全')
                    if info.file_size > MAX_MEMBER_SIZE:
                        _fail('备份包成员过多或过大')
                    total += info.file_size
                    if total > MAX_TOTAL_SIZE:
                        _fail('备份包成员过多或过大')
                    if info.file_size and (info.compress_size == 0 or
                                           info.file_size / info.compress_size > MAX_COMPRESSION_RATIO):
                        _fail('备份包成员过多或过大')
                if 'manifest.json' not in names:
                    _fail('备份包格式无效')
                try:
                    with archive.open('manifest.json') as raw:
                        manifest = json.loads(raw.read(MAX_MEMBER_SIZE + 1).decode('utf-8'))
                except (KeyError, UnicodeError, json.JSONDecodeError):
                    _fail('备份包格式无效')
                files, database_row = self._validate_manifest(manifest, names)
                for row in files:
                    digest = hashlib.sha256()
                    count = 0
                    try:
                        with archive.open(row['path']) as raw:
                            while True:
                                block = raw.read(_CHUNK)
                                if not block:
                                    break
                                count += len(block)
                                if count > row['size'] or count > MAX_MEMBER_SIZE:
                                    _fail('备份包校验失败')
                                digest.update(block)
                    except (KeyError, RuntimeError, zipfile.BadZipFile, EOFError):
                        _fail('备份包校验失败')
                    if count != row['size'] or digest.hexdigest() != row['sha256']:
                        _fail('备份包校验失败')
                fd, temp_name = tempfile.mkstemp(suffix='.sqlite3')
                os.close(fd)
                database_temp = Path(temp_name)
                with archive.open(database_row['path']) as raw, database_temp.open('wb') as target:
                    shutil.copyfileobj(raw, target, _CHUNK)
            try:
                database_validation = self.store.validate_backup(database_temp)
            except Exception:
                _fail('备份包数据库无效')
            if not isinstance(database_validation, dict) or database_validation.get('valid') is not True:
                _fail('备份包数据库无效')
            warnings = []
            if any(row['kind'] == 'vault' for row in files):
                warnings.append('密钥库为DPAPI密文，只能在原Windows账号和系统条件下解密')
            return {'ok': True, 'manifest': manifest,
                    'database_validation': database_validation,
                    'files_count': len(files), 'total_size': sum(row['size'] for row in files),
                    'warnings': warnings}
        except ValueError as exc:
            if str(exc) in SAFE_BUNDLE_ERRORS:
                raise
            _fail('备份包格式无效')
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError):
            _fail('备份包格式无效')
        finally:
            if database_temp is not None:
                try:
                    database_temp.unlink(missing_ok=True)
                except OSError:
                    pass

    def _disable_schedules(self, database):
        try:
            with sqlite3.connect(database) as db:
                rows = list(db.execute("SELECT id,data FROM objects WHERE kind='schedules'"))
                for oid, encoded in rows:
                    value = json.loads(encoded)
                    value['enabled'] = False
                    db.execute("UPDATE objects SET data=? WHERE kind='schedules' AND id=?",
                               (json.dumps(value, ensure_ascii=False), oid))
                db.execute('INSERT INTO audit(time,action,object_id,detail) VALUES(?,?,?,?)',
                           (datetime.now(timezone.utc).isoformat(), '离线恢复', 'database',
                            '所有周期计划已停用，请核对发送记录后重新启用'))
        except Exception:
            _fail('恢复失败')

    def restore(self, path, target_data_dir, confirm=False, service_stopped=False):
        """Restore a validated bundle by atomically replacing the target tree."""
        if confirm is not True:
            _fail('恢复需明确确认')
        if service_stopped is not True:
            _fail('恢复前必须停止服务')
        inspected = self.inspect(path)
        try:
            target = Path(target_data_dir).resolve(strict=False)
            root = self.root.resolve(strict=False)
            if target == target.parent or target.exists() and not target.is_dir():
                _fail('目标目录无效')
            # The explicit offline assertion is mandatory for every target,
            # especially when replacing this service's live root.
            if target == root and service_stopped is not True:
                _fail('恢复前必须停止服务')
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                for item in target.rglob('*'):
                    if item.is_symlink():
                        _fail('目标目录无效')
        except ValueError as exc:
            if str(exc) in SAFE_BUNDLE_ERRORS:
                raise
            _fail('目标目录无效')
        except Exception:
            _fail('目标目录无效')

        stamp = datetime.now().strftime('%Y%m%d%H%M%S')
        stage = target.parent / ('.' + target.name + '.restore-' + uuid4().hex)
        rollback = target.parent / ('.' + target.name + '.rollback-' + uuid4().hex)
        moved_old = False
        installed = False
        preserved = []
        try:
            if target.exists():
                shutil.copytree(target, stage)
            else:
                stage.mkdir()
            old_db = stage / 'itmanager.sqlite3'
            if old_db.is_file():
                saved = stage / ('itmanager.sqlite3.before-restore-' + stamp + '.bak')
                shutil.copy2(old_db, saved)
                preserved.append(str(target / saved.name))
            old_attachments = stage / 'attachments'
            if old_attachments.is_dir():
                saved = stage / ('attachments.before-restore-' + stamp)
                shutil.copytree(old_attachments, saved)
                preserved.append(str(target / saved.name))
                shutil.rmtree(old_attachments)
            old_vault = stage / 'secrets' / 'vault.json'
            if old_vault.is_file():
                saved = old_vault.with_name('vault.json.before-restore-' + stamp + '.bak')
                shutil.copy2(old_vault, saved)
                preserved.append(str(target / 'secrets' / saved.name))
                old_vault.unlink()

            manifest = inspected['manifest']
            rows = {row['path']: row for row in manifest['files']}
            with zipfile.ZipFile(path, 'r') as archive:
                for member, row in rows.items():
                    relative = {'database': 'itmanager.sqlite3',
                                'attachment': member,
                                'vault': member}[row['kind']]
                    destination = stage / PurePosixPath(relative)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, destination.open('wb') as output:
                        shutil.copyfileobj(source, output, _CHUNK)
            self._disable_schedules(stage / 'itmanager.sqlite3')
            # Validate once more after schedule/audit mutation and before install.
            validation = self.store.validate_backup(stage / 'itmanager.sqlite3')
            if not isinstance(validation, dict) or validation.get('valid') is not True:
                _fail('恢复失败')
            if target.exists():
                os.replace(target, rollback)
                moved_old = True
            try:
                os.replace(stage, target)
            except OSError:
                # Some Windows filesystems refuse directory replacement even
                # within one volume.  The old tree remains in ``rollback``;
                # install by copy and keep rollback available until complete.
                shutil.copytree(stage, target)
                # Best-effort stage cleanup happens in ``finally``.  Windows
                # may briefly retain a SQLite handle after read-only checking.
            installed = True
            if moved_old:
                shutil.rmtree(rollback)
            return {'restored': str(target), 'preserved': preserved,
                    'schedules_disabled': True, 'manifest': manifest}
        except ValueError as exc:
            if moved_old and not installed and rollback.exists():
                try:
                    if target.exists():
                        shutil.rmtree(target)
                    os.replace(rollback, target)
                except OSError:
                    pass
            if str(exc) in SAFE_BUNDLE_ERRORS:
                raise
            _fail('恢复失败')
        except Exception:
            if moved_old and rollback.exists():
                try:
                    # A fallback copy may have created a partial target before
                    # failing.  Remove it, then put the untouched old tree back.
                    if target.exists():
                        shutil.rmtree(target)
                    os.replace(rollback, target)
                except OSError:
                    pass
            _fail('恢复失败')
        finally:
            if stage.exists():
                try:
                    shutil.rmtree(stage)
                except OSError:
                    pass
            if installed and rollback.exists():
                try:
                    shutil.rmtree(rollback)
                except OSError:
                    pass

    def list_bundles(self, folder):
        """List ZIP metadata; malformed bundles are represented, never raised."""
        try:
            base = Path(folder)
            paths = sorted(base.glob('*.zip'), key=lambda item: item.name)
        except Exception:
            return []
        result = []
        for path in paths:
            try:
                inspected = self.inspect(path)
                result.append({'name': path.name, 'path': str(path), 'size': path.stat().st_size,
                               'ok': True, 'created': inspected['manifest']['created'],
                               'manifest': inspected['manifest'],
                               'files_count': inspected['files_count'],
                               'total_size': inspected['total_size'],
                               'warnings': inspected['warnings']})
            except Exception as exc:
                message = str(exc) if str(exc) in SAFE_BUNDLE_ERRORS else '备份包格式无效'
                result.append({'name': path.name, 'path': str(path),
                               'size': path.stat().st_size if path.is_file() else 0,
                               'ok': False, 'error': message})
        return result
