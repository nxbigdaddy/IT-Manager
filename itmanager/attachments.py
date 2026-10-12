"""Quarantined evidence-attachment store: opaque bytes on the server data disk.

Design invariants (the whole point of this module):

* Uploaded content is treated as *opaque bytes only*. It is never executed,
  never unpacked, never parsed, never previewed and never reopened by content
  type. Nothing here guesses a MIME type or hands bytes to a viewer.
* Only fixed, self-authored error strings ever escape; no user input is ever
  concatenated into a message. Every raising path uses a value from
  ``SAFE_ATTACHMENT_ERRORS``.
* No arbitrary path read or write: the caller supplies a record id and a
  display name, never a target path. The stored path is always
  ``<root>/attachments/<record_id>/<attachment_id><suffix>`` and the final
  resolved path is re-checked to still sit under ``<root>/attachments``.
* Filenames are display metadata only. They never create directories, never
  influence the stored path, and their suffix is checked against an allow-list
  that deliberately excludes executables and scripts.

Ownership split with the caller (server/GUI layer):

* This module writes attachment *binaries* and owns their integrity hashes.
* The caller owns *metadata persistence* (database/JSON). It may pass a
  ``loader`` (``callable(record_id) -> list[dict]``) so that notes, actors and
  hashes survive a restart, and it must persist the dict returned by ``add``
  via :func:`metadata_to_state`. Helpers :func:`attach` (persist-or-rollback)
  and :meth:`AttachmentStore.remember` / :meth:`AttachmentStore.forget` are
  provided for that wiring.

Wiring note: hash verification (tamper detection) depends on persisted
metadata. Without a ``loader`` a freshly started process still lists and reads
attachment bytes, but :meth:`AttachmentStore.verify` deliberately reports
``ok=False`` because no recorded hash is available to compare against. The
server layer must therefore always construct the store with a loader that
returns the rows it persisted from :func:`metadata_to_state`.

``stored`` is a POSIX-style *relative* path such as
``attachments/TASK-ab12/9f8e...c.pdf``. Relative paths are the recommended
persisted form: an absolute path would embed the original data directory and
break backup/restore onto another machine.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


# Every message this module can raise. Fixed text, never interpolated.
SAFE_ATTACHMENT_ERRORS = frozenset({
    '附件名称无效',
    '附件内容为空或超过大小限制',
    '附件类型不允许',
    '附件数量已达上限',
    '附件不存在',
    '附件记录已损坏',
    '附件存储不可用',
    '附件归属记录无效',
    '附件说明过长',
})

# 10 MiB, matching the notification layer's per-file email ceiling.
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PER_RECORD = 20
MAX_NOTE = 500
MAX_NAME = 120
MAX_RECORD_ID = 200
MAX_ACTOR = 120

# Evidence formats an IT manager actually attaches. Explicitly absent:
# exe dll bat cmd ps1 js vbs scr msi lnk jar and every other live payload.
ALLOWED_SUFFIXES = frozenset({
    # documents
    'pdf', 'txt', 'md', 'csv', 'rtf',
    'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'odt', 'ods', 'odp',
    # images
    'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'tif', 'tiff', 'heic',
    # logs and structured evidence
    'log', 'json', 'xml', 'yaml', 'yml', 'ini', 'conf', 'cfg', 'eml', 'msg',
    # archives and disk images (stored opaque, never unpacked here)
    'zip', '7z', 'rar', 'gz', 'tgz', 'bz2', 'xz', 'tar', 'iso',
    # packet captures occasionally filed as evidence
    'pcap', 'pcapng',
})

# Refused even if a future allow-list edit drifts: executables, scripts,
# installers, shortcuts and libraries that a double click would run.
BLOCKED_SUFFIXES = frozenset({
    'exe', 'dll', 'bat', 'cmd', 'ps1', 'psm1', 'js', 'mjs', 'cjs', 'jse',
    'vbs', 'vbe', 'wsf', 'wsh', 'ws', 'scr', 'msi', 'msp', 'mst', 'lnk',
    'jar', 'com', 'pif', 'cpl', 'hta', 'reg', 'sys', 'drv', 'apk', 'app',
    'deb', 'rpm', 'dmg', 'sh', 'bash', 'zsh', 'py', 'pyc', 'pyo', 'rb',
    'pl', 'php', 'asp', 'aspx', 'jsp', 'cgi', 'gadget', 'inf', 'scf',
    'url', 'job', 'vb', 'vbscript', 'msc', 'ocx', 'cab',
})

_RELATIVE_ROOT = 'attachments'
_RECORD_ID_RE = re.compile(r'[A-Za-z0-9_-]{1,%d}' % MAX_RECORD_ID)
_ATTACHMENT_ID_RE = re.compile(r'[0-9a-f]{32}')
_SHA256_RE = re.compile(r'[0-9a-f]{64}')
_SUFFIX_RE = re.compile(r'[a-z0-9]{1,12}')
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f-\x9f]')
_DISPLAY_BAD_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f]')

METADATA_KEYS = ('id', 'record_id', 'name', 'suffix', 'size', 'sha256',
                 'note', 'actor', 'created', 'stored')


def human_size(size):
    """Format a byte count for display; invalid input reports ``0 B``."""
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return '0 B'
    if size < 1024:
        return '%d B' % size
    value = float(size)
    for unit in ('KiB', 'MiB', 'GiB', 'TiB', 'PiB'):
        value /= 1024.0
        if value < 1024.0 or unit == 'PiB':
            break
    if value < 10:
        return '%.1f %s' % (value, unit)
    return '%.0f %s' % (value, unit)


def safe_display_name(name, fallback='未命名附件', limit=MAX_NAME):
    """Strip paths and dangerous characters for UI display only.

    Never used to build a storage path. Returns ``fallback`` when nothing safe
    survives, so a UI label can never be empty or path-like.
    """
    if not isinstance(name, str):
        return fallback
    text = name.replace('\\', '/').split('/')[-1]
    text = _DISPLAY_BAD_RE.sub('_', text).strip().strip('. ')
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        limit = MAX_NAME
    if len(text) > limit:
        text = text[:limit]
    return text or fallback


def _suffix_of(name):
    """Final-extension suffix without the dot, or ``None``.

    The *final* dot decides: ``report.pdf.`` and ``noextension`` have no
    usable suffix, and a suffix with path or shell characters is rejected
    here so no separator can ever reach a stored filename.
    """
    index = name.rfind('.')
    if index <= 0 or index == len(name) - 1:
        return None
    suffix = name[index + 1:].lower()
    if not _SUFFIX_RE.fullmatch(suffix) or '/' in suffix or '\\' in suffix:
        return None
    return suffix


def _validate_record_id(record_id, message='附件归属记录无效'):
    if not isinstance(record_id, str) or not record_id or len(record_id) > MAX_RECORD_ID:
        raise ValueError(message)
    if not _RECORD_ID_RE.fullmatch(record_id):
        raise ValueError(message)
    return record_id


def _clean_filename(filename):
    """Reduce any caller-supplied name to a safe display name, or raise.

    Absolute paths and ``..`` segments are *sanitized away* (only the basename
    survives) rather than trusted; the result is a bare filename that is never
    used to build a path. A missing or malformed suffix is '附件类型不允许';
    a name that survives as nothing usable is '附件名称无效'.
    """
    if not isinstance(filename, str) or '\x00' in filename:
        raise ValueError('附件名称无效')
    name = filename.replace('\\', '/').split('/')[-1]
    name = _CONTROL_RE.sub('', name).strip()
    name = name.strip('. ')
    if not name or name in {'.', '..'} or '..' in name:
        raise ValueError('附件名称无效')
    if len(name) > MAX_NAME:
        raise ValueError('附件名称无效')
    suffix = _suffix_of(name)
    if suffix is None:
        # Trailing dot or a suffix full of path characters: unusable name.
        if name.endswith('.') or not _SUFFIX_RE.fullmatch(name[name.rfind('.') + 1:].lower()):
            raise ValueError('附件名称无效')
        raise ValueError('附件类型不允许')
    if suffix in BLOCKED_SUFFIXES or suffix not in ALLOWED_SUFFIXES:
        raise ValueError('附件类型不允许')
    return name, suffix


def _now():
    return datetime.now(timezone.utc).isoformat()


class AttachmentStore:
    """Controlled evidence attachment storage rooted at a server data directory.

    ``root`` is the server data directory (``Path`` or ``str``). Construction is
    side-effect free: no directory is created and no file is written until
    ``add`` is called.

    ``loader`` is an optional ``callable(record_id) -> list[dict]`` supplied by
    the caller so that persisted metadata (note, actor, sha256, created) is the
    authority for reads, listing and integrity checks. Without one, the store
    falls back to metadata remembered in-process by :meth:`remember`; with
    neither, listings are rebuilt from the files themselves (names and sizes
    only) and :meth:`read` cannot detect tampering.
    """

    def __init__(self, root, loader=None):
        if isinstance(root, Path):
            self.root = root
        elif isinstance(root, str):
            if not root or '\x00' in root:
                raise ValueError('附件存储不可用')
            self.root = Path(root)
        else:
            raise ValueError('附件存储不可用')
        if loader is not None and not callable(loader):
            raise ValueError('附件存储不可用')
        self.loader = loader
        self._lock = threading.RLock()
        self._memory = {}

    # ------------------------------------------------------------ path guards
    @property
    def base(self):
        """Configured ``<root>/attachments`` directory (may not exist yet)."""
        return self.root / _RELATIVE_ROOT

    def _validated_base(self):
        """Resolve ``root/attachments`` and prove it stays under ``root``."""
        try:
            root = self.root.resolve(strict=False)
            base = (self.root / _RELATIVE_ROOT).resolve(strict=False)
            base.relative_to(root)
        except (OSError, ValueError, RuntimeError):
            raise ValueError('附件存储不可用') from None
        if base == root:
            raise ValueError('附件存储不可用')
        return base

    def _record_dir(self, record_id):
        base = self._validated_base()
        target = (base / record_id).resolve(strict=False)
        try:
            target.relative_to(base)
        except ValueError:
            raise ValueError('附件存储不可用') from None
        if target == base:
            raise ValueError('附件存储不可用')
        return target

    @staticmethod
    def relative_path(record_id, attachment_id, suffix):
        """Canonical stored relative path for one attachment (POSIX separators)."""
        return '%s/%s/%s.%s' % (_RELATIVE_ROOT, record_id, attachment_id, str(suffix).lower())

    def path_of(self, metadata):
        """Absolute path for one metadata row, re-derived from trusted ids.

        Only the identity fields (record id, attachment id, suffix) are used;
        ``stored`` is advisory and a forged value is ignored rather than
        followed, so a hand-edited row can never redirect a read outside the
        store.
        """
        row = self._path_row(metadata)
        base = self._validated_base()
        target = (base / row['record_id'] / ('%s.%s' % (row['id'], row['suffix']))).resolve(strict=False)
        try:
            target.relative_to(base)
        except ValueError:
            raise ValueError('附件存储不可用') from None
        return target

    @staticmethod
    def _path_row(row):
        """Validate only the fields that decide a path; raise '附件记录已损坏'."""
        if not isinstance(row, dict):
            raise ValueError('附件记录已损坏')
        record_id = row.get('record_id')
        attachment_id = row.get('id')
        suffix = row.get('suffix')
        if not isinstance(record_id, str) or not _RECORD_ID_RE.fullmatch(record_id):
            raise ValueError('附件记录已损坏')
        if not isinstance(attachment_id, str) or not _ATTACHMENT_ID_RE.fullmatch(attachment_id):
            raise ValueError('附件记录已损坏')
        if not isinstance(suffix, str) or not _SUFFIX_RE.fullmatch(suffix):
            raise ValueError('附件记录已损坏')
        if suffix not in ALLOWED_SUFFIXES or suffix in BLOCKED_SUFFIXES:
            raise ValueError('附件类型不允许')
        return {'id': attachment_id, 'record_id': record_id, 'suffix': suffix}

    @staticmethod
    def _valid_row(row):
        """Validate one full metadata row, or raise '附件记录已损坏'.

        Returns a normalized copy so downstream code never trusts raw input.
        """
        identity = AttachmentStore._path_row(row)
        record_id, attachment_id, suffix = identity['record_id'], identity['id'], identity['suffix']
        size = row.get('size')
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > MAX_FILE_BYTES:
            raise ValueError('附件记录已损坏')
        sha256 = row.get('sha256')
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise ValueError('附件记录已损坏')
        note = row.get('note', '')
        if not isinstance(note, str) or len(note) > MAX_NOTE:
            raise ValueError('附件记录已损坏')
        name = row.get('name')
        if not isinstance(name, str) or not name:
            name = '%s.%s' % (attachment_id, suffix)
        actor = row.get('actor', '')
        actor = actor if isinstance(actor, str) else ''
        created = row.get('created', '')
        created = created if isinstance(created, str) else ''
        return {
            'id': attachment_id,
            'record_id': record_id,
            'name': name,
            'suffix': suffix,
            'size': size,
            'sha256': sha256,
            'note': note,
            'actor': actor,
            'created': created,
            'stored': AttachmentStore.relative_path(record_id, attachment_id, suffix),
        }

    # --------------------------------------------------------- metadata hooks
    def remember(self, metadata):
        """Cache persisted metadata in-process (used when no ``loader`` exists)."""
        row = self._valid_row(metadata)
        with self._lock:
            self._memory.setdefault(row['record_id'], {})[row['id']] = row
        return row

    def forget(self, record_id, attachment_id):
        """Drop in-process metadata for a deleted attachment."""
        with self._lock:
            rows = self._memory.get(record_id)
            if rows:
                rows.pop(attachment_id, None)

    def metadata(self, record_id):
        """Authoritative persisted metadata rows for one record.

        Rows that fail validation are skipped: a corrupt metadata row must never
        become a path or a hash check. Rows are returned oldest-first.
        """
        if not isinstance(record_id, str) or not _RECORD_ID_RE.fullmatch(record_id or ''):
            return []
        raw = []
        if self.loader is not None:
            try:
                loaded = self.loader(record_id)
            except Exception:
                raise ValueError('附件存储不可用') from None
            if loaded is None:
                loaded = []
            if not isinstance(loaded, list):
                raise ValueError('附件记录已损坏')
            raw = loaded
        else:
            with self._lock:
                raw = list(self._memory.get(record_id, {}).values())
        rows = []
        for item in raw:
            try:
                row = self._valid_row(item)
            except ValueError:
                continue
            if row['record_id'] != record_id:
                continue
            rows.append(row)
        rows.sort(key=lambda row: (row['created'], row['id']))
        return rows

    def _known_ids(self, record_id):
        return {row['id'] for row in self.metadata(record_id)}

    # ------------------------------------------------------------- public API
    def add(self, record_id, filename, content, note='', actor=''):
        """Persist one attachment binary and return its metadata dict.

        The caller persists the returned metadata (see :func:`attach` for a
        persist-or-rollback wrapper). Content is stored verbatim and is never
        inspected, executed, extracted or previewed.
        """
        record_id = _validate_record_id(record_id)
        name, suffix = _clean_filename(filename)
        if type(content) is not bytes:
            raise ValueError('附件内容为空或超过大小限制')
        if not content or len(content) > MAX_FILE_BYTES:
            raise ValueError('附件内容为空或超过大小限制')
        if not isinstance(note, str) or len(note) > MAX_NOTE:
            raise ValueError('附件说明过长')
        actor = actor if isinstance(actor, str) else ''
        actor = _CONTROL_RE.sub('', actor).strip()[:MAX_ACTOR]

        with self._lock:
            existing = self.metadata(record_id)
            if len(existing) >= MAX_PER_RECORD:
                raise ValueError('附件数量已达上限')
            base = self._validated_base()
            directory = self._record_dir(record_id)
            attachment_id = uuid4().hex
            relative = self.relative_path(record_id, attachment_id, suffix)
            target = (directory / ('%s.%s' % (attachment_id, suffix))).resolve(strict=False)
            try:
                target.relative_to(base)
            except ValueError:
                raise ValueError('附件存储不可用') from None
            if target.is_symlink() or target.is_dir():
                raise ValueError('附件存储不可用')
            temp = directory / ('.tmp-' + uuid4().hex)
            try:
                directory.mkdir(parents=True, exist_ok=True)
                if directory.is_symlink() or not directory.is_dir():
                    raise OSError('record directory unusable')
                # Re-check after mkdir: the directory must still sit under base.
                directory.resolve(strict=False).relative_to(base)
                with open(temp, 'xb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Atomic publish: readers never observe a partial attachment.
                os.replace(temp, target)
            except ValueError:
                raise
            except OSError:
                try:
                    if temp.exists() or temp.is_symlink():
                        temp.unlink()
                except OSError:
                    pass
                raise ValueError('附件存储不可用') from None
            metadata = {
                'id': attachment_id,
                'record_id': record_id,
                'name': name,
                'suffix': suffix,
                'size': len(content),
                'sha256': hashlib.sha256(content).hexdigest(),
                'note': note,
                'actor': actor,
                'created': _now(),
                'stored': relative,
            }
            # Keep the freshly written hash available to read/verify even when
            # the caller has not persisted metadata yet. A caller-supplied
            # loader always wins, and the caller's persisted row is the durable
            # authority across restarts.
            self.remember(metadata)
        return metadata

    def list(self, record_id):
        """Metadata rows for one record, oldest first; ``[]`` on bad input.

        Uses caller-persisted metadata when available and otherwise rebuilds a
        name/size-only view from the files on disk. Symlinks, directories and
        temporary files are never reported.
        """
        if not isinstance(record_id, str) or not _RECORD_ID_RE.fullmatch(record_id or ''):
            return []
        try:
            directory = self._record_dir(record_id)
        except ValueError:
            return []
        if directory.is_symlink() or not directory.is_dir():
            return []
        rows = self.metadata(record_id)
        known = {}
        for entry in self._disk_entries(record_id):
            known[entry['id']] = entry
        result = []
        for row in rows:
            entry = known.pop(row['id'], None)
            if entry is None:
                # Metadata without a file: binary lost, do not advertise it.
                continue
            result.append(dict(row, size=entry['size']))
        result.extend(known.values())
        result.sort(key=lambda row: (row['created'], row['id']))
        return result

    def _disk_entries(self, record_id):
        """Files actually present, with sizes and mtimes; never hashes."""
        try:
            directory = self._record_dir(record_id)
        except ValueError:
            return []
        if directory.is_symlink() or not directory.is_dir():
            return []
        entries = []
        try:
            scanned = list(os.scandir(directory))
        except OSError:
            return []
        for entry in scanned:
            name = entry.name
            if name.startswith('.tmp-'):
                continue
            stem, _, suffix = name.rpartition('.')
            suffix = suffix.lower()
            if not _ATTACHMENT_ID_RE.fullmatch(stem) or suffix not in ALLOWED_SUFFIXES:
                continue
            if suffix in BLOCKED_SUFFIXES:
                continue
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            entries.append({
                'id': stem,
                'record_id': record_id,
                'name': '%s.%s' % (stem, suffix),
                'suffix': suffix,
                'size': stat.st_size,
                'sha256': '',
                'note': '',
                'actor': '',
                'created': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                'stored': self.relative_path(record_id, stem, suffix),
            })
        return entries

    def _locate(self, record_id, attachment_id):
        """Resolve one attachment to (metadata, path) or raise a safe error."""
        _validate_record_id(record_id, '附件不存在')
        if not isinstance(attachment_id, str) or not _ATTACHMENT_ID_RE.fullmatch(attachment_id):
            raise ValueError('附件不存在')
        match = next((row for row in self.list(record_id) if row['id'] == attachment_id), None)
        if match is None:
            raise ValueError('附件不存在')
        try:
            path = self.path_of(match)
        except ValueError:
            raise ValueError('附件不存在') from None
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError('附件不存在')
        except OSError:
            raise ValueError('附件不存在') from None
        return match, path

    def _digest(self, path):
        digest = hashlib.sha256()
        total = 0
        with open(path, 'rb') as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise ValueError('附件记录已损坏')
                digest.update(chunk)
        return digest.hexdigest(), total

    def read(self, record_id, attachment_id):
        """Return stored bytes after size and hash verification.

        Raises '附件不存在' when the metadata or file is missing, and
        '附件记录已损坏' when size or digest disagrees with the recorded hash.

        If no persisted metadata is loaded (no ``loader`` and no in-process
        record) the size check still applies, but a hash cannot be checked;
        wire the loader so this path is never used in production.
        """
        match, path = self._locate(record_id, attachment_id)
        try:
            data = path.read_bytes()
        except OSError:
            raise ValueError('附件不存在') from None
        if len(data) != match['size']:
            raise ValueError('附件记录已损坏')
        if not match['sha256']:
            # Unverifiable row: never claim a hash check that did not happen.
            return data
        if hashlib.sha256(data).hexdigest() != match['sha256']:
            raise ValueError('附件记录已损坏')
        return data

    def verify(self, record_id, attachment_id):
        """Integrity self-check for the evidence audit trail.

        Returns ``{'ok': bool, 'sha256': str, 'size': int}``. Missing metadata
        or file still raises '附件不存在'; a mismatch reports ``ok=False``.
        """
        match, path = self._locate(record_id, attachment_id)
        try:
            digest, size = self._digest(path)
        except (OSError, ValueError):
            return {'ok': False, 'sha256': match['sha256'], 'size': match['size']}
        return {'ok': bool(match['sha256']) and digest == match['sha256'] and size == match['size'],
                'sha256': digest, 'size': size}

    def delete(self, record_id, attachment_id):
        """Unlink one attachment binary; a missing file is not a failure.

        Metadata removal belongs to the caller (:meth:`forget` drops the
        in-process copy); the metadata row disappearing is what makes a later
        ``read`` report '附件不存在'.
        """
        _validate_record_id(record_id, '附件不存在')
        if not isinstance(attachment_id, str) or not _ATTACHMENT_ID_RE.fullmatch(attachment_id):
            raise ValueError('附件不存在')
        matched = [row for row in self.metadata(record_id) if row['id'] == attachment_id]
        if not matched:
            raise ValueError('附件不存在')
        for row in matched:
            try:
                path = self.path_of(row)
            except ValueError:
                raise ValueError('附件不存在') from None
            try:
                if path.is_symlink() or path.is_file():
                    path.unlink()
            except OSError:
                # Already gone or concurrently removed: not a failure.
                pass
        self.forget(record_id, attachment_id)
        return {'deleted': True, 'id': attachment_id}


def attach(store, record_id, filename, content, note='', actor='', persist=None):
    """``add`` + caller persistence with rollback, for HTTP/GUI wiring.

    ``persist(metadata)`` is the caller's metadata writer. If it raises, the
    just-written binary is removed and '附件存储不可用' is raised, so a failed
    metadata write never leaves an unreferenced orphan file.
    """
    if not isinstance(store, AttachmentStore):
        raise ValueError('附件存储不可用')
    metadata = store.add(record_id, filename, content, note=note, actor=actor)
    try:
        if persist is not None:
            persist(dict(metadata))
        store.remember(metadata)
    except Exception:
        try:
            store.delete(record_id, metadata['id'])
        except ValueError:
            pass
        raise ValueError('附件存储不可用') from None
    return metadata


def metadata_to_state(metadata):
    """Normalize one attachment metadata dict into a JSON-serializable shape.

    Returns a *new* plain dict with exactly ``METADATA_KEYS``; raises
    ``ValueError('附件记录已损坏')`` for a row that is not a usable record.
    Callers persist this verbatim (inside a record's ``data`` or through
    ``Store.put('attachments', ...)``).
    """
    row = AttachmentStore._valid_row(metadata)
    name = safe_display_name(row['name'])
    return {
        'id': row['id'],
        'record_id': row['record_id'],
        'name': name,
        'suffix': row['suffix'],
        'size': row['size'],
        'sha256': row['sha256'],
        'note': row['note'],
        'actor': row['actor'],
        'created': row['created'],
        # Persist the RELATIVE path: absolute paths break backup/restore.
        'stored': row['stored'],
    }


def state_from_metadata(state):
    """Inverse of :func:`metadata_to_state`; raises on corrupt or forged rows."""
    return metadata_to_state(state)
