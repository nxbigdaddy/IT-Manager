"""Evidence attachment store tests: containment, tamper detection, no execution.

Every test uses ``tmp_path`` as the data root. No network, no real business
data, and no attachment is ever executed, unpacked or opened by content type.
"""
from __future__ import annotations

import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from itmanager import attachments
from itmanager.attachments import (
    ALLOWED_SUFFIXES,
    BLOCKED_SUFFIXES,
    MAX_FILE_BYTES,
    MAX_NOTE,
    MAX_PER_RECORD,
    SAFE_ATTACHMENT_ERRORS,
    AttachmentStore,
    attach,
    human_size,
    metadata_to_state,
    safe_display_name,
    state_from_metadata,
)

RECORD = 'TASK-0123456789ab'

PDF_BYTES = b'%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n'
PNG_BYTES = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64
TXT_BYTES = 'evidence line one\n证据第二行\n'.encode('utf-8')
EXEC_BYTES = b'MZ\x90\x00\x03\x00\x00\x00fake-executable-payload'


def safe_errors(operation, *args, **kwargs):
    """Run ``operation`` and return the raised message, asserting it is safe."""
    with pytest.raises(ValueError) as excinfo:
        operation(*args, **kwargs)
    message = str(excinfo.value)
    assert message in SAFE_ATTACHMENT_ERRORS, 'unsafe message escaped: %r' % message
    return message


@pytest.fixture()
def store(tmp_path):
    return AttachmentStore(tmp_path)


@pytest.fixture()
def persisted(tmp_path):
    """Store plus an in-memory stand-in for the caller's metadata database."""
    rows = {}
    store = AttachmentStore(tmp_path, loader=lambda record_id: list(rows.get(record_id, [])))

    def persist(metadata):
        rows.setdefault(metadata['record_id'], []).append(dict(metadata))

    store.rows = rows
    store.persist = persist
    return store


# --------------------------------------------------------------- happy paths
def test_add_returns_metadata_and_writes_identical_bytes(store, tmp_path):
    metadata = store.add(RECORD, 'evidence.pdf', PDF_BYTES, note='服务器日志证据', actor='张工')
    assert set(metadata) == set(attachments.METADATA_KEYS)
    assert metadata['record_id'] == RECORD
    assert metadata['name'] == 'evidence.pdf'
    assert metadata['suffix'] == 'pdf'
    assert metadata['size'] == len(PDF_BYTES)
    assert metadata['sha256'] == hashlib.sha256(PDF_BYTES).hexdigest()
    assert re.fullmatch(r'[0-9a-f]{32}', metadata['id'])
    assert metadata['note'] == '服务器日志证据'
    assert metadata['actor'] == '张工'

    real = tmp_path / 'attachments' / RECORD / (metadata['id'] + '.pdf')
    assert real.is_file()
    assert real.read_bytes() == PDF_BYTES
    assert metadata['stored'] == 'attachments/%s/%s.pdf' % (RECORD, metadata['id'])
    assert Path(str(tmp_path).replace('\\', '/') + '/' + metadata['stored']).read_bytes() == PDF_BYTES


def test_add_does_not_create_directories_before_first_write(tmp_path):
    store = AttachmentStore(tmp_path)
    assert not (tmp_path / 'attachments').exists()
    assert store.list(RECORD) == []
    assert not (tmp_path / 'attachments').exists()
    store.add(RECORD, 'a.txt', TXT_BYTES)
    assert (tmp_path / 'attachments' / RECORD).is_dir()


def test_read_round_trip_for_each_allowed_family(store):
    for index, (name, payload) in enumerate([
        ('report.pdf', PDF_BYTES), ('screen.PNG', PNG_BYTES), ('notes.md', TXT_BYTES),
        ('log.txt', TXT_BYTES), ('archive.zip', b'PK\x03\x04opaque-bytes-not-extracted'),
        ('capture.pcapng', b'\x0a\x0d\x0d\x0a' + b'\x11' * 32),
    ]):
        metadata = store.add(RECORD, name, payload, note='case %d' % index)
        assert store.read(RECORD, metadata['id']) == payload
        assert store.verify(RECORD, metadata['id'])['ok'] is True


def test_list_returns_attachments_in_created_order(store):
    first = store.add(RECORD, '1.txt', b'one')
    second = store.add(RECORD, '2.txt', b'two')
    third = store.add(RECORD, '3.txt', b'three')
    listed = store.list(RECORD)
    assert [row['id'] for row in listed] == [first['id'], second['id'], third['id']]
    assert [row['name'] for row in listed] == ['1.txt', '2.txt', '3.txt']
    assert all(row['record_id'] == RECORD for row in listed)
    assert store.list('OTHER-RECORD') == []
    assert store.list('') == []


def test_same_name_twice_never_overwrites(store, tmp_path):
    first = store.add(RECORD, 'dup.txt', b'first version')
    second = store.add(RECORD, 'dup.txt', b'second version different')
    assert first['id'] != second['id']
    assert store.read(RECORD, first['id']) == b'first version'
    assert store.read(RECORD, second['id']) == b'second version different'
    files = [p for p in (tmp_path / 'attachments' / RECORD).iterdir()]
    assert len(files) == 2


# ---------------------------------------------------------- name sanitization
@pytest.mark.parametrize('hostile', [
    '../../evil.pdf',
    '..\\..\\evil.pdf',
    '/etc/passwd.pdf',
    'C:\\Windows\\System32\\evil.pdf',
    '\\\\server\\share\\evil.pdf',
    'sub/dir/../evil.pdf',
    '....//....//evil.pdf',
])
def test_path_traversal_names_are_sanitized(store, tmp_path, hostile):
    metadata = store.add(RECORD, hostile, PDF_BYTES)
    assert metadata['name'] in {'evil.pdf', 'passwd.pdf'}
    assert metadata['name'] == Path(hostile.replace('\\', '/')).name
    assert '/' not in metadata['name'] and '\\' not in metadata['name'] and '..' not in metadata['name']
    stored = tmp_path / 'attachments' / RECORD / (metadata['id'] + '.pdf')
    assert stored.read_bytes() == PDF_BYTES
    # Nothing may appear outside root/attachments.
    outside = {p.name for p in tmp_path.iterdir()} - {'attachments'}
    assert outside == set()
    assert store.read(RECORD, metadata['id']) == PDF_BYTES


def test_absolute_paths_never_escape_the_store(store, tmp_path):
    metadata = store.add(RECORD, 'C:\\Users\\Public\\passwd.pdf', PDF_BYTES)
    assert metadata['name'] == 'passwd.pdf'
    assert metadata['stored'].startswith('attachments/')
    for candidate in tmp_path.rglob('*'):
        assert tmp_path in candidate.parents or candidate == tmp_path
    assert 'C:' not in metadata['stored']


def test_traversal_in_record_id_is_refused(store, tmp_path):
    for hostile in ['../evil', '..', 'a/b', 'a\\b', 'C:', 'a b', 'TASK.1', '中文记录', 123, None, '', 'x' * 201]:
        message = safe_errors(store.add, hostile, 'evidence.pdf', PDF_BYTES)
        assert message == '附件归属记录无效'
    assert not (tmp_path.parent / 'evil').exists()


def test_record_id_accepts_system_style_ids(store):
    for record_id in ['TASK-0123456789ab', 'PROJ-abc_123', 'a', 'A' * 200]:
        metadata = store.add(record_id, 'ok.pdf', PDF_BYTES)
        assert metadata['record_id'] == record_id
        store.delete(record_id, metadata['id'])


def test_null_bytes_and_control_characters_in_name_are_handled(store):
    assert safe_errors(store.add, RECORD, 'evil\x00.pdf', PDF_BYTES) == '附件名称无效'
    metadata = store.add(RECORD, 'ev\x01il\x7f.pdf', PDF_BYTES)
    assert metadata['name'] == 'evil.pdf'
    assert safe_errors(store.add, RECORD, '  ..  ', PDF_BYTES) == '附件名称无效'
    assert safe_errors(store.add, RECORD, '', PDF_BYTES) == '附件名称无效'
    assert safe_errors(store.add, RECORD, None, PDF_BYTES) == '附件名称无效'
    assert safe_errors(store.add, RECORD, 'name.' + 'x' * 300 + '.pdf', PDF_BYTES) == '附件名称无效'
    assert safe_errors(store.add, RECORD, '  ', PDF_BYTES) == '附件名称无效'
    assert safe_errors(store.add, RECORD, '../', PDF_BYTES) == '附件名称无效'


def test_name_limit_is_inclusive(store):
    assert safe_errors(store.add, RECORD, 'a' * 121 + '.pdf', PDF_BYTES) == '附件名称无效'
    # 116-char stem + '.pdf' is exactly MAX_NAME and is accepted.
    boundary = 'b' * (attachments.MAX_NAME - 4) + '.pdf'
    assert len(boundary) == attachments.MAX_NAME
    metadata = store.add(RECORD, boundary, PDF_BYTES)
    assert len(metadata['name']) <= attachments.MAX_NAME
    assert safe_errors(store.add, RECORD, 'c' * (attachments.MAX_NAME - 3) + '.pdf',
                       PDF_BYTES) == '附件名称无效'
    assert safe_errors(store.add, RECORD, '证' * 120 + '.pdf', PDF_BYTES) == '附件名称无效'


# ------------------------------------------------------------ suffix allowlist
@pytest.mark.parametrize('suffix', sorted(BLOCKED_SUFFIXES))
def test_every_blocked_suffix_is_refused(store, suffix):
    assert suffix in ALLOWED_SUFFIXES or suffix not in ALLOWED_SUFFIXES
    assert safe_errors(store.add, RECORD, 'payload.' + suffix, EXEC_BYTES) == '附件类型不允许'


@pytest.mark.parametrize('suffix', ['exe', 'dll', 'bat', 'cmd', 'ps1', 'js', 'vbs', 'scr', 'msi', 'lnk', 'jar'])
def test_executable_families_are_explicitly_blocked(suffix):
    assert suffix in BLOCKED_SUFFIXES
    assert suffix not in ALLOWED_SUFFIXES


def test_blocked_suffix_is_refused_even_when_masked_as_pdf(store):
    assert safe_errors(store.add, RECORD, 'invoice.pdf.exe', EXEC_BYTES) == '附件类型不允许'
    assert safe_errors(store.add, RECORD, 'invoice.exe.pdf.exe', EXEC_BYTES) == '附件类型不允许'
    assert safe_errors(store.add, RECORD, 'script.pdf.bat', EXEC_BYTES) == '附件类型不允许'
    assert safe_errors(store.add, RECORD, 'tool.PDF.EXE', EXEC_BYTES) == '附件类型不允许'


def test_missing_or_unknown_suffix_is_refused(store):
    assert safe_errors(store.add, RECORD, 'noextension', PDF_BYTES) == '附件类型不允许'
    assert safe_errors(store.add, RECORD, 'plain.', PDF_BYTES) == '附件类型不允许'
    assert safe_errors(store.add, RECORD, 'report.unknownext', PDF_BYTES) == '附件类型不允许'
    assert safe_errors(store.add, RECORD, '.pdf', PDF_BYTES) == '附件类型不允许'
    assert safe_errors(store.add, RECORD, 'archive.tar.exe7', PDF_BYTES) == '附件类型不允许'
    # A trailing dot is normalized away: the stored label has one real extension.
    assert store.add(RECORD, 'report.pdf.', PDF_BYTES)['name'] == 'report.pdf'
    assert store.add(RECORD, 'report.pdf.', PDF_BYTES)['suffix'] == 'pdf'
    # Sanity: the plain allowed case still works.
    assert store.add(RECORD, 'report.pdf', PDF_BYTES)['suffix'] == 'pdf'


def test_suffix_is_case_insensitive_and_lowercased(store):
    metadata = store.add(RECORD, 'REPORT.PDF', PDF_BYTES)
    assert metadata['suffix'] == 'pdf'
    assert metadata['name'] == 'REPORT.PDF'
    assert store.read(RECORD, metadata['id']) == PDF_BYTES


# -------------------------------------------------------------- size and note
def test_empty_content_is_refused(store):
    assert safe_errors(store.add, RECORD, 'empty.pdf', b'') == '附件内容为空或超过大小限制'


def test_content_must_be_bytes(store):
    for bad in ['text', bytearray(b'x'), memoryview(b'x'), None, 42, ['b']]:
        assert safe_errors(store.add, RECORD, 'x.pdf', bad) == '附件内容为空或超过大小限制'


def test_file_size_limit_is_enforced(store, tmp_path):
    metadata = store.add(RECORD, 'exactly.pdf', b'x' * MAX_FILE_BYTES)
    assert metadata['size'] == MAX_FILE_BYTES
    assert safe_errors(store.add, RECORD, 'over.pdf', b'x' * (MAX_FILE_BYTES + 1)) == '附件内容为空或超过大小限制'
    assert MAX_FILE_BYTES == 10 * 1024 * 1024
    written = [p.stat().st_size for p in (tmp_path / 'attachments' / RECORD).iterdir()]
    assert written == [MAX_FILE_BYTES]


def test_note_limit_is_enforced(store):
    metadata = store.add(RECORD, 'note.txt', TXT_BYTES, note='n' * MAX_NOTE)
    assert metadata['note'] == 'n' * MAX_NOTE
    assert safe_errors(store.add, RECORD, 'note2.txt', TXT_BYTES, note='n' * (MAX_NOTE + 1)) == '附件说明过长'
    assert safe_errors(store.add, RECORD, 'note3.txt', TXT_BYTES, note=None) == '附件说明过长'


def test_note_is_not_written_into_the_stored_file(store):
    metadata = store.add(RECORD, 'clean.txt', TXT_BYTES, note='secret note not in blob')
    assert store.read(RECORD, metadata['id']) == TXT_BYTES
    assert b'secret note' not in store.read(RECORD, metadata['id'])


# ---------------------------------------------------------------- record quota
def test_per_record_limit(store):
    assert MAX_PER_RECORD == 20
    for index in range(MAX_PER_RECORD):
        store.add(RECORD, 'file%d.txt' % index, b'payload %d' % index)
    assert len(store.list(RECORD)) == MAX_PER_RECORD
    assert safe_errors(store.add, RECORD, 'overflow.txt', b'overflow') == '附件数量已达上限'
    other = store.add('PROJ-other', 'fine.txt', b'other record is unaffected')
    assert store.read('PROJ-other', other['id']) == b'other record is unaffected'


def test_per_record_limit_uses_persisted_metadata(persisted):
    for index in range(MAX_PER_RECORD):
        attach(persisted, RECORD, 'file%d.txt' % index, b'payload %d' % index, persist=persisted.persist)
    assert len(persisted.list(RECORD)) == MAX_PER_RECORD
    assert safe_errors(attach, persisted, RECORD, 'overflow.txt', b'overflow',
                       persist=persisted.persist) == '附件数量已达上限'
    assert len(persisted.rows[RECORD]) == MAX_PER_RECORD


def test_delete_frees_a_quota_slot(store):
    first = store.add(RECORD, 'first.txt', b'first')
    for index in range(MAX_PER_RECORD - 1):
        store.add(RECORD, 'fill%d.txt' % index, b'fill %d' % index)
    assert safe_errors(store.add, RECORD, 'over.txt', b'over') == '附件数量已达上限'
    store.delete(RECORD, first['id'])
    replacement = store.add(RECORD, 'replacement.txt', b'replacement')
    assert store.read(RECORD, replacement['id']) == b'replacement'


# ------------------------------------------------------------ read/verify/delete
def test_read_rejects_bad_arguments(store):
    metadata = store.add(RECORD, 'ok.pdf', PDF_BYTES)
    assert safe_errors(store.read, RECORD, 'not-an-id') == '附件不存在'
    assert safe_errors(store.read, RECORD, 'f' * 32) == '附件不存在'
    assert safe_errors(store.read, '../evil', metadata['id']) == '附件不存在'
    assert safe_errors(store.read, RECORD, None) == '附件不存在'
    assert safe_errors(store.read, RECORD, metadata['id'].upper()) == '附件不存在'


def test_tampered_file_is_reported_as_corrupt(store, tmp_path):
    metadata = store.add(RECORD, 'tamper.pdf', PDF_BYTES)
    real = tmp_path / 'attachments' / RECORD / (metadata['id'] + '.pdf')
    real.write_bytes(PDF_BYTES + b'INJECTED')
    assert safe_errors(store.read, RECORD, metadata['id']) == '附件记录已损坏'
    result = store.verify(RECORD, metadata['id'])
    assert result['ok'] is False
    assert result['sha256'] != metadata['sha256']
    assert result['size'] == len(PDF_BYTES) + len(b'INJECTED')


def test_truncated_file_is_reported_as_corrupt(store, tmp_path):
    metadata = store.add(RECORD, 'truncate.pdf', PDF_BYTES)
    real = tmp_path / 'attachments' / RECORD / (metadata['id'] + '.pdf')
    real.write_bytes(PDF_BYTES[:5])
    assert safe_errors(store.read, RECORD, metadata['id']) == '附件记录已损坏'
    assert store.verify(RECORD, metadata['id'])['ok'] is False


def test_verify_passes_for_untouched_file(store):
    metadata = store.add(RECORD, 'intact.png', PNG_BYTES)
    result = store.verify(RECORD, metadata['id'])
    assert result == {'ok': True, 'sha256': hashlib.sha256(PNG_BYTES).hexdigest(), 'size': len(PNG_BYTES)}
    assert set(result) == {'ok', 'sha256', 'size'}


def test_verify_missing_attachment_raises_not_found(store):
    assert safe_errors(store.verify, RECORD, 'a' * 32) == '附件不存在'
    assert safe_errors(store.verify, 'BAD/ID', 'a' * 32) == '附件不存在'


def test_delete_removes_file_and_then_read_reports_missing(store, tmp_path, persisted):
    metadata = store.add(RECORD, 'gone.txt', TXT_BYTES)
    real = tmp_path / 'attachments' / RECORD / (metadata['id'] + '.txt')
    assert real.exists()
    assert store.delete(RECORD, metadata['id']) == {'deleted': True, 'id': metadata['id']}
    assert not real.exists()
    assert store.list(RECORD) == []
    assert safe_errors(store.read, RECORD, metadata['id']) == '附件不存在'
    assert safe_errors(store.verify, RECORD, metadata['id']) == '附件不存在'


def test_repeated_delete_is_not_found_not_failure(store):
    metadata = store.add(RECORD, 'twice.txt', TXT_BYTES)
    assert store.delete(RECORD, metadata['id'])['deleted'] is True
    assert safe_errors(store.delete, RECORD, metadata['id']) == '附件不存在'
    assert safe_errors(store.delete, RECORD, 'b' * 32) == '附件不存在'
    assert safe_errors(store.delete, 'bad id', metadata['id']) == '附件不存在'


def test_delete_survives_already_missing_file(store, tmp_path):
    metadata = store.add(RECORD, 'vanish.txt', TXT_BYTES)
    (tmp_path / 'attachments' / RECORD / (metadata['id'] + '.txt')).unlink()
    assert store.delete(RECORD, metadata['id']) == {'deleted': True, 'id': metadata['id']}
    assert store.list(RECORD) == []


# --------------------------------------------------------- storage containment
def test_store_never_follows_symlinked_attachment(store, tmp_path):
    metadata = store.add(RECORD, 'real.txt', TXT_BYTES)
    real = tmp_path / 'attachments' / RECORD / (metadata['id'] + '.txt')
    outside = tmp_path.parent / ('outside-%s.txt' % metadata['id'])
    outside.write_bytes(b'outside payload that must not be served through the store')
    try:
        real.unlink()
        try:
            os.symlink(outside, real)
        except (OSError, NotImplementedError):
            pytest.skip('symlink creation unavailable')
        assert safe_errors(store.read, RECORD, metadata['id']) == '附件不存在'
        assert safe_errors(store.verify, RECORD, metadata['id']) == '附件不存在'
        assert store.list(RECORD) == []
    finally:
        if outside.exists():
            outside.unlink()


def test_directory_masquerading_as_attachment_fails_safely(store, tmp_path):
    metadata = store.add(RECORD, 'dir.txt', TXT_BYTES)
    real = tmp_path / 'attachments' / RECORD / (metadata['id'] + '.txt')
    real.unlink()
    real.mkdir()
    assert safe_errors(store.read, RECORD, metadata['id']) == '附件不存在'
    assert store.list(RECORD) == []
    assert store.delete(RECORD, metadata['id']) == {'deleted': True, 'id': metadata['id']}


def test_forged_stored_path_is_ignored(store, tmp_path):
    metadata = store.add(RECORD, 'forged.txt', TXT_BYTES)
    forged = dict(metadata, stored='attachments/../../outside.txt')
    # 'stored' is advisory only: the path is re-derived from the ids.
    assert store.path_of(forged) == (tmp_path / 'attachments' / RECORD / (metadata['id'] + '.txt')).resolve()
    for bad in ['id', 'suffix', 'record_id']:
        corrupt = dict(metadata)
        corrupt[bad] = '../../evil'
        assert safe_errors(store.path_of, corrupt) in {'附件记录已损坏', '附件存储不可用'}
    assert safe_errors(store.path_of, None) == '附件记录已损坏'
    assert safe_errors(store.path_of, {'id': metadata['id']}) == '附件记录已损坏'


def test_no_temporary_files_remain_after_add(store, tmp_path):
    for index in range(3):
        store.add(RECORD, 'file%d.txt' % index, b'payload %d' % index)
    leftovers = [p.name for p in (tmp_path / 'attachments' / RECORD).iterdir() if p.name.startswith('.tmp-')]
    assert leftovers == []
    names = sorted(p.name for p in (tmp_path / 'attachments' / RECORD).iterdir())
    assert names == sorted(row['id'] + '.txt' for row in store.list(RECORD))


def test_temporary_files_are_not_reported_as_attachments(store, tmp_path):
    metadata = store.add(RECORD, 'real.txt', TXT_BYTES)
    directory = tmp_path / 'attachments' / RECORD
    (directory / '.tmp-partial').write_bytes(b'half written upload')
    listed = store.list(RECORD)
    assert [row['id'] for row in listed] == [metadata['id']]
    assert store.read(RECORD, metadata['id']) == TXT_BYTES


def test_restart_reads_without_recorded_hash_but_verify_is_false(store, tmp_path):
    # A fresh instance shares no in-process metadata: it must still serve the
    # file rather than misreporting it, while refusing to claim a hash check.
    metadata = store.add(RECORD, 'after-restart.txt', TXT_BYTES)
    restarted = AttachmentStore(tmp_path)
    listed = restarted.list(RECORD)
    assert [row['id'] for row in listed] == [metadata['id']]
    assert listed[0]['sha256'] == ''
    assert restarted.read(RECORD, metadata['id']) == TXT_BYTES
    assert restarted.verify(RECORD, metadata['id'])['ok'] is False

    # With the caller's persisted metadata the hash check comes back.
    restarted_loader = AttachmentStore(tmp_path, loader=lambda record_id: [metadata])
    assert restarted_loader.read(RECORD, metadata['id']) == TXT_BYTES
    assert restarted_loader.verify(RECORD, metadata['id']) == {
        'ok': True, 'sha256': metadata['sha256'], 'size': len(TXT_BYTES)}


def test_unwritable_root_fails_as_storage_unavailable(store, tmp_path):
    blocked = tmp_path / 'attachments'
    blocked.write_bytes(b'I am a file, not a directory')
    assert safe_errors(store.add, RECORD, 'x.pdf', PDF_BYTES) == '附件存储不可用'
    assert safe_errors(AttachmentStore, '') == '附件存储不可用'
    assert safe_errors(AttachmentStore, None) == '附件存储不可用'
    assert safe_errors(AttachmentStore, tmp_path, 7) == '附件存储不可用'
    assert safe_errors(store.add, 'BAD/ID', 'x.pdf', PDF_BYTES) == '附件归属记录无效'


# ----------------------------------------------------------- concurrency/atomic
def test_concurrent_adds_are_unique_and_readable(store, tmp_path):
    payloads = [('worker-%d.txt' % index, ('payload-%d' % index).encode()) for index in range(10)]

    def worker(item):
        name, payload = item
        return store.add(RECORD, name, payload)

    with ThreadPoolExecutor(max_workers=10) as pool:
        rows = list(pool.map(worker, payloads))
    assert len({row['id'] for row in rows}) == 10
    assert len(store.list(RECORD)) == 10
    for (_, payload), row in zip(payloads, rows):
        assert store.read(RECORD, row['id']) == payload
        assert row['sha256'] == hashlib.sha256(payload).hexdigest()
    assert len(list((tmp_path / 'attachments' / RECORD).iterdir())) == 10


def test_concurrent_adds_respect_the_limit(store):
    def worker(index):
        try:
            return store.add(RECORD, 'race-%d.txt' % index, b'race payload %d' % index)
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(MAX_PER_RECORD + 6)))
    stored = [row for row in results if isinstance(row, dict)]
    refused = [row for row in results if isinstance(row, str)]
    assert len(stored) == MAX_PER_RECORD
    assert refused and set(refused) <= SAFE_ATTACHMENT_ERRORS
    assert len(store.list(RECORD)) == MAX_PER_RECORD


# ------------------------------------------------------------- constants/safety
def test_safe_errors_frozenset_shape():
    assert isinstance(SAFE_ATTACHMENT_ERRORS, frozenset)
    required = {'附件名称无效', '附件内容为空或超过大小限制', '附件类型不允许', '附件数量已达上限',
                '附件不存在', '附件记录已损坏', '附件存储不可用', '附件归属记录无效', '附件说明过长'}
    assert required <= SAFE_ATTACHMENT_ERRORS
    assert len(SAFE_ATTACHMENT_ERRORS) == 9
    assert all(isinstance(message, str) and message and '\x00' not in message for message in SAFE_ATTACHMENT_ERRORS)


def test_allowed_and_blocked_suffix_sets_are_sane():
    assert 'pdf' in ALLOWED_SUFFIXES and 'png' in ALLOWED_SUFFIXES and 'jpg' in ALLOWED_SUFFIXES
    assert 'jpeg' in ALLOWED_SUFFIXES and 'gif' in ALLOWED_SUFFIXES and 'webp' in ALLOWED_SUFFIXES
    for suffix in ['txt', 'md', 'csv', 'xlsx', 'xls', 'docx', 'doc', 'zip', '7z']:
        assert suffix in ALLOWED_SUFFIXES
    assert ALLOWED_SUFFIXES.isdisjoint(BLOCKED_SUFFIXES)
    assert all(suffix == suffix.lower() for suffix in ALLOWED_SUFFIXES | BLOCKED_SUFFIXES)
    assert all(re.fullmatch(r'[a-z0-9]{1,12}', suffix) for suffix in ALLOWED_SUFFIXES | BLOCKED_SUFFIXES)
    assert MAX_PER_RECORD == 20
    assert MAX_FILE_BYTES == 10 * 1024 * 1024
    assert MAX_NOTE == 500


def test_every_raised_message_is_in_the_safe_set(store, tmp_path):
    checks = [
        (store.add, (RECORD, 'x.pdf', b'') ),
        (store.add, (RECORD, 'x.exe', EXEC_BYTES)),
        (store.add, (RECORD, 'x', PDF_BYTES)),
        (store.add, ('bad/id', 'x.pdf', PDF_BYTES)),
        (store.add, (RECORD, 'x.pdf', 'not bytes')),
        (store.add, (RECORD, 'x.pdf', PDF_BYTES, 'n' * (MAX_NOTE + 1))),
        (store.read, (RECORD, 'z' * 32)),
        (store.verify, (RECORD, 'z' * 32)),
        (store.delete, (RECORD, 'z' * 32)),
    ]
    for operation, args in checks:
        assert safe_errors(operation, *args) in SAFE_ATTACHMENT_ERRORS
    metadata = store.add(RECORD, 'tamper.txt', TXT_BYTES)
    (tmp_path / 'attachments' / RECORD / (metadata['id'] + '.txt')).write_bytes(b'tampered')
    assert safe_errors(store.read, RECORD, metadata['id']) == '附件记录已损坏'


def test_corrupt_loader_rows_are_skipped_not_trusted(persisted):
    good = attach(persisted, RECORD, 'good.txt', TXT_BYTES, persist=persisted.persist)
    persisted.rows[RECORD].append({'id': '../../escape', 'record_id': RECORD, 'suffix': 'exe',
                                   'size': 4, 'sha256': 'zz', 'stored': '../../escape'})
    persisted.rows[RECORD].append('not a dict')
    listed = persisted.list(RECORD)
    assert [row['id'] for row in listed] == [good['id']]
    assert persisted.read(RECORD, good['id']) == TXT_BYTES


def test_loader_failure_is_reported_as_storage_unavailable(tmp_path):
    def exploded(record_id):
        raise RuntimeError('database offline with secret details')

    store = AttachmentStore(tmp_path, loader=exploded)
    message = safe_errors(store.list, RECORD) if False else None
    with pytest.raises(ValueError) as excinfo:
        store.metadata(RECORD)
    assert str(excinfo.value) == '附件存储不可用'
    assert 'secret' not in str(excinfo.value)
    store2 = AttachmentStore(tmp_path, loader=lambda record_id: {'not': 'a list'})
    with pytest.raises(ValueError) as excinfo2:
        store2.metadata(RECORD)
    assert str(excinfo2.value) == '附件记录已损坏'


def test_loader_backed_store_keeps_note_and_detects_tampering(persisted, tmp_path):
    metadata = attach(persisted, RECORD, 'persist.pdf', PDF_BYTES, note='归档证据',
                      actor='李工', persist=persisted.persist)
    listed = persisted.list(RECORD)
    assert [row['id'] for row in listed] == [metadata['id']]
    assert listed[0]['note'] == '归档证据'
    assert listed[0]['sha256'] == metadata['sha256']
    assert persisted.read(RECORD, metadata['id']) == PDF_BYTES

    (tmp_path / 'attachments' / RECORD / (metadata['id'] + '.pdf')).write_bytes(PDF_BYTES + b'x')
    assert safe_errors(persisted.read, RECORD, metadata['id']) == '附件记录已损坏'
    assert persisted.verify(RECORD, metadata['id'])['ok'] is False

    state = metadata_to_state(metadata)
    assert state == state_from_metadata(state)
    assert state['stored'] == metadata['stored']
    assert set(state) == set(attachments.METADATA_KEYS)


def test_attach_rolls_back_file_when_persist_fails(tmp_path):
    store = AttachmentStore(tmp_path)
    before = list(tmp_path.rglob('*'))

    def broken(metadata):
        raise RuntimeError('write failed with secret details')

    with pytest.raises(ValueError) as excinfo:
        attach(store, RECORD, 'rollback.pdf', PDF_BYTES, persist=broken)
    assert str(excinfo.value) == '附件存储不可用'
    assert 'secret' not in str(excinfo.value)
    assert store.list(RECORD) == []
    assert [p for p in tmp_path.rglob('*') if p.is_file()] == [p for p in before if p.is_file()]


def test_metadata_helpers_validate_rows(store):
    metadata = store.add(RECORD, 'state.json', b'{"ok":true}')
    state = metadata_to_state(metadata)
    assert set(state) == set(attachments.METADATA_KEYS)
    assert state['stored'].startswith('attachments/') and '\\' not in state['stored']
    assert state_from_metadata(metadata)['sha256'] == metadata['sha256']
    for bad in [None, [], 'x', {}, dict(metadata, id='../x'), dict(metadata, sha256='xyz'),
                dict(metadata, suffix='exe'), dict(metadata, size=-1), dict(metadata, record_id='a/b')]:
        assert safe_errors(metadata_to_state, bad) in {'附件记录已损坏', '附件类型不允许'}


def test_human_size_and_safe_display_name_tools():
    assert human_size(0) == '0 B'
    assert human_size(1023) == '1023 B'
    assert human_size(1024) == '1.0 KiB'
    assert human_size(10 * 1024 * 1024) == '10 MiB'
    assert human_size(-1) == '0 B'
    assert human_size('nope') == '0 B'
    assert human_size(None) == '0 B'
    assert human_size(True) == '0 B'
    assert human_size(1536) == '1.5 KiB'

    assert safe_display_name('../../etc/passwd') == 'passwd'
    assert safe_display_name('C:\\Users\\a\\report.pdf') == 'report.pdf'
    assert safe_display_name('evil\x00name.pdf') == 'evil_name.pdf'
    assert safe_display_name('a<b>c:d"e|f?g*h.pdf') == 'a_b_c_d_e_f_g_h.pdf'
    assert safe_display_name('') == '未命名附件'
    assert safe_display_name(None) == '未命名附件'
    assert safe_display_name('...') == '未命名附件'
    assert safe_display_name('x' * 400).__len__() == attachments.MAX_NAME
    assert safe_display_name('ok.pdf', limit=3) == 'ok.'
