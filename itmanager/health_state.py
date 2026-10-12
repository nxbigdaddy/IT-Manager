"""Pure data-health handling state over health.scan findings: no IO, no clock, no writes.

A state object records what a person decided about one finding (resolved /
task_created / ignored) so the same defect stops reappearing as new work. This
module only computes keys, validates, merges and filters; persistence is the
caller's job (``store.put``/``store.get``) using :func:`finding_key` as the object id.

Findings are keyed by (code, record_id, field) exactly as produced by
``health.scan``, and a state whose ``record_version`` no longer matches the
finding's record is treated as stale so the defect reappears. Every failure uses a
fixed message from ``SAFE_HEALTH_STATE_ERRORS`` without interpolating user values.
"""
from __future__ import annotations

import hashlib
import json
import re

# Fixed user-facing errors; never interpolate untrusted values into exceptions.
SAFE_HEALTH_STATE_ERRORS = frozenset({
    '整改状态无效', '整改状态包含未知字段', '误报忽略必须填写原因', '整改说明过长',
    '整改状态记录版本无效', '整改状态键无效', '整改任务ID无效',
})

STATUSES = ('resolved', 'task_created', 'ignored')

_STATE_KEYS = frozenset({'code', 'record_id', 'field', 'status', 'reason', 'task_id',
                         'record_version', 'actor', 'updated'})
_PART = re.compile(r'[A-Za-z0-9_-]+')
_TASK_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
_KEYS_PREFIX = 'hs-'
_MAX_KEY_LEN = 120
_MAX_REASON = 500
_MAX_ACTOR = 100
_DIGEST_LEN = 32


def _invalid() -> ValueError:
    """Return the fixed key error rather than raising, to keep call sites tidy."""
    return ValueError('整改状态键无效')


def _digest(*parts) -> str:
    payload = json.dumps([list(parts), [len(p) for p in parts]], ensure_ascii=False,
                         separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]


def _ascii(value) -> bool:
    return isinstance(value, str) and bool(_PART.fullmatch(value))


def _encoded(value) -> str:
    """Encode one part as object-id-safe ASCII, keeping chinese field names unique."""
    if _ascii(value):
        return value
    return 'u' + hashlib.sha256(value.encode('utf-8')).hexdigest()


def finding_key(code, record_id, field) -> str:
    """Return one stable id for (code, record_id, field), usable as an object id.

    The result always matches ``[A-Za-z0-9_-]`` and is at most 120 characters, and
    the same finding parts always yield the same key. Health ``field`` values are
    usually Chinese schema names (负责人, 截止日期 ...): non-ASCII parts are encoded
    to a deterministic ASCII token so real findings still get usable ids. Only a
    non-string, blank or whitespace-padded part raises ValueError.
    """
    parts = (code, record_id, field)
    for part in parts:
        if not isinstance(part, str) or not part.strip() or part != part.strip():
            raise _invalid()
    encoded = tuple(_encoded(part) for part in parts)
    key = 'hs-c-' + encoded[0] + '-i-' + encoded[1] + '-f-' + encoded[2]
    if len(key) <= _MAX_KEY_LEN:
        return key
    # Parts too long to spell out in full: keep a readable ASCII head plus a
    # digest of the whole triple, so uniqueness never depends on a truncation.
    head = ('c-' + encoded[0][:_MAX_KEY_LEN // 5] + '-i-' + encoded[1][:_MAX_KEY_LEN // 5]
            + '-f-' + encoded[2][:_MAX_KEY_LEN // 5])
    room = _MAX_KEY_LEN - len(_KEYS_PREFIX) - len(head) - 1
    if room < 16:
        head = head[:_MAX_KEY_LEN - len(_KEYS_PREFIX) - 17]
        room = 16
    return _KEYS_PREFIX + head + '-' + _digest(*parts)[:room]


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def validate_state(data) -> None:
    """Raise ValueError with a fixed SAFE_HEALTH_STATE_ERRORS message, or return None."""
    if not isinstance(data, dict) or set(data) - _STATE_KEYS:
        raise ValueError('整改状态包含未知字段')
    code, record_id, field = data.get('code'), data.get('record_id'), data.get('field')
    if not isinstance(code, str) or not code:
        raise ValueError('整改状态键无效')
    if not isinstance(record_id, str) or not record_id:
        raise ValueError('整改状态键无效')
    if not isinstance(field, str) or not field:
        raise ValueError('整改状态键无效')
    try:
        finding_key(code, record_id, field)
    except ValueError:
        raise ValueError('整改状态键无效') from None
    status = data.get('status')
    if not isinstance(status, str) or status not in STATUSES:
        raise ValueError('整改状态无效')
    reason = data.get('reason', '')
    if not isinstance(reason, str) or len(reason) > _MAX_REASON:
        raise ValueError('整改说明过长')
    if status == 'ignored' and not reason.strip():
        raise ValueError('误报忽略必须填写原因')
    task_id = data.get('task_id')
    if task_id is not None and (not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id)):
        raise ValueError('整改任务ID无效')
    record_version = data.get('record_version')
    if record_version is not None and (type(record_version) is not int or record_version < 1):
        raise ValueError('整改状态记录版本无效')
    actor = data.get('actor', '')
    if not isinstance(actor, str) or len(actor) > _MAX_ACTOR:
        raise ValueError('整改状态包含未知字段')
    updated = data.get('updated', '')
    if not isinstance(updated, str) or len(updated) > 64:
        raise ValueError('整改状态包含未知字段')


def _item_state_key(finding) -> str:
    """Return the state key a finding/state maps to, or None when it cannot be keyed."""
    try:
        return finding_key(_text(finding.get('code')), _text(finding.get('record_id')),
                           _text(finding.get('field')))
    except ValueError:
        return None


def _state_sort(states):
    """Index for 'latest updated wins', keeping input order when updated is absent."""
    return sorted(states, key=lambda pair: (_text(pair[1].get('updated')), pair[0]))


def _latest_states(states):
    """One state per (code, record_id, field): newest ``updated`` wins, else later input."""
    if not isinstance(states, list):
        return {}
    kept = {}
    for index, raw in enumerate(states):
        if not isinstance(raw, dict):
            continue
        item_key = _item_state_key(raw)
        if item_key is None:
            continue
        kept.setdefault(item_key, []).append((index, raw))
    result = {}
    for item_key, group in kept.items():
        result[item_key] = _state_sort(group)[-1][1]
    return result


def _same_version(state, finding):
    """True when the state still matches the finding's record version.

    Findings without ``record_version`` skip the staleness check entirely; states
    without a version never expire.
    """
    if 'record_version' not in finding:
        return True
    expected = state.get('record_version')
    if expected is None:
        return True
    return finding.get('record_version') == expected


def apply_states(findings, states, include_resolved=False) -> list[dict]:
    """Merge handling states into scan findings and filter the visible list.

    Rules: one state per (code, record_id, field) with the newest ``updated``
    (later input order breaks ties); a state whose ``record_version`` no longer
    matches the finding is stale and the finding reappears; ``resolved``,
    ``task_created`` and ``ignored`` hide the finding unless ``include_resolved``
    marks it ``handled`` with that status. Finding order and every other field are
    preserved untouched. Non-list input is handled safely as an empty result.
    """
    if not isinstance(findings, list):
        return []
    latest = _latest_states(states)
    result = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        finding_key_value = _item_state_key(finding)
        state = latest.get(finding_key_value) if finding_key_value is not None else None
        if state is None or not _same_version(state, finding):
            result.append(dict(finding))
            continue
        status = state.get('status')
        if status not in STATUSES:
            # Unknown status (unvalidated input) never hides a finding.
            result.append(dict(finding))
            continue
        if include_resolved:
            marked = dict(finding)
            marked['handled'] = status
            result.append(marked)
    return result


def summarize_states(findings) -> dict:
    """Count findings by handling marker: total/active/task_created/resolved/ignored."""
    result = {'total': 0, 'active': 0, 'task_created': 0, 'resolved': 0, 'ignored': 0}
    for finding in findings or []:
        if not isinstance(finding, dict):
            continue
        result['total'] += 1
        handled = finding.get('handled')
        if handled in STATUSES:
            result[handled] += 1
        else:
            result['active'] += 1
    return result
