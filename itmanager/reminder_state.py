"""Pure reminder handling state over compute_items output: no IO, no clock, no writes.

A state object records what a person decided about one computed reminder item
(done / snooze / dismiss) so the same unsolved item stops reappearing. This
module only computes keys, validates, merges and filters; persistence is the
caller's job (``store.put``/``store.get``) using :func:`state_key` as the object id.

Callers pass ``today`` explicitly so filtering stays reproducible. Every failure
uses a fixed message from ``SAFE_STATE_ERRORS`` without interpolating user values.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date

# Fixed user-facing errors; never interpolate untrusted values into exceptions.
SAFE_STATE_ERRORS = frozenset({
    '提醒处理动作无效', '提醒处理状态包含未知字段', '稍后提醒必须给出未来日期',
    '忽略提醒必须填写原因', '提醒处理原因过长', '提醒处理状态记录版本无效',
    '提醒处理状态键无效',
})

ACTIONS = ('done', 'snooze', 'dismiss')

_STATE_KEYS = frozenset({'rule_key', 'record_id', 'action', 'reason', 'until',
                         'record_version', 'actor', 'updated'})
_PART = re.compile(r'[A-Za-z0-9_-]+')
_KEYS_PREFIX = 'rs-'
_MAX_KEY_LEN = 120
_MAX_REASON = 500
_MAX_ACTOR = 100


def _invalid() -> ValueError:
    """Return the fixed key error rather than raising, to keep call sites tidy."""
    return ValueError('提醒处理状态键无效')


def _digest(*parts) -> str:
    payload = json.dumps([list(parts), [len(p) for p in parts]], ensure_ascii=False,
                         separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]


def _ascii(value) -> bool:
    return isinstance(value, str) and bool(_PART.fullmatch(value))


def state_key(rule_key, record_id) -> str:
    """Return one stable id for (rule_key, record_id), usable directly as an object id.

    Output matches ``[A-Za-z0-9_-]`` and is at most 120 characters; the same rule
    key and record id always yield the same key. Non-string, blank or
    out-of-charset input raises ValueError.
    """
    for part in (rule_key, record_id):
        if not isinstance(part, str) or not _ascii(part) or part != part.strip():
            raise _invalid()
    key = 'rs-r-' + rule_key + '-i-' + record_id
    if len(key) <= _MAX_KEY_LEN:
        return key
    # Ids too long to spell out in full: keep a readable ASCII head plus a digest
    # of the whole pair, so uniqueness never depends on a truncated prefix.
    head = 'r-' + rule_key[:_MAX_KEY_LEN // 4] + '-i-' + record_id[:_MAX_KEY_LEN // 4]
    room = _MAX_KEY_LEN - len(_KEYS_PREFIX) - len(head) - 1
    if room < 16:
        head = head[:_MAX_KEY_LEN - len(_KEYS_PREFIX) - 17]
        room = 16
    return _KEYS_PREFIX + head + '-' + _digest(rule_key, record_id)[:room]


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def validate_state(data, today=None) -> None:
    """Raise ValueError with a fixed SAFE_STATE_ERRORS message, or return None."""
    if not isinstance(data, dict) or set(data) - _STATE_KEYS:
        raise ValueError('提醒处理状态包含未知字段')
    rule_key, record_id = data.get('rule_key'), data.get('record_id')
    if not isinstance(rule_key, str) or not rule_key:
        raise ValueError('提醒处理状态键无效')
    if not isinstance(record_id, str) or not record_id:
        raise ValueError('提醒处理状态键无效')
    try:
        state_key(rule_key, record_id)
    except ValueError:
        raise ValueError('提醒处理状态键无效') from None
    action = data.get('action')
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError('提醒处理动作无效')
    reason = data.get('reason', '')
    if not isinstance(reason, str) or len(reason) > _MAX_REASON:
        raise ValueError('提醒处理原因过长')
    if action == 'dismiss' and not reason.strip():
        raise ValueError('忽略提醒必须填写原因')
    if action == 'snooze':
        until = data.get('until')
        if not isinstance(until, str):
            raise ValueError('稍后提醒必须给出未来日期')
        try:
            due = date.fromisoformat(until)
        except ValueError:
            raise ValueError('稍后提醒必须给出未来日期') from None
        if today is None:
            today = date.today()
        elif type(today) is not date:
            raise ValueError('稍后提醒必须给出未来日期')
        if due <= today:
            raise ValueError('稍后提醒必须给出未来日期')
    record_version = data.get('record_version')
    if record_version is not None and (type(record_version) is not int or record_version < 1):
        raise ValueError('提醒处理状态记录版本无效')
    actor = data.get('actor', '')
    if not isinstance(actor, str) or len(actor) > _MAX_ACTOR:
        raise ValueError('提醒处理状态包含未知字段')
    updated = data.get('updated', '')
    if not isinstance(updated, str) or len(updated) > 64:
        raise ValueError('提醒处理状态包含未知字段')


def _item_state_key(item) -> str:
    """Return the state key an item/state maps to, or None when it cannot be keyed."""
    try:
        return state_key(_text(item.get('rule_key')), _text(item.get('record_id')))
    except ValueError:
        return None


def _state_sort(states):
    """Index for 'latest updated wins', keeping input order when updated is absent."""
    return sorted(states, key=lambda pair: (_text(pair[1].get('updated')), pair[0]))


def _latest_states(states):
    """One state per (rule_key, record_id): newest ``updated`` wins, else the later input."""
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


def _parse_date(value):
    """Parse a strict YYYY-MM-DD value; return None when unusable."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _same_version(state, item):
    """True when the state still matches the item's record version.

    Items without ``record_version`` skip the staleness check entirely; states
    without a version never expire.
    """
    if 'record_version' not in item:
        return True
    expected = state.get('record_version')
    if expected is None:
        return True
    return item.get('record_version') == expected


def apply_states(items, states, today, include_handled=False) -> list[dict]:
    """Merge handling states into computed reminder items and filter the visible list.

    Rules: one state per (rule_key, record_id) with the newest ``updated`` (later
    input order breaks ties); a state whose ``record_version`` no longer matches
    the item is stale and the reminder reappears; ``snooze`` hides while
    ``until > today`` and reappears with ``resumed=True`` once ``until <= today``;
    ``done``/``dismiss`` hide unless ``include_handled`` marks them
    ``handled='done'``/``'dismiss'``. Item order and item keys never change; only
    the ``handled``/``resumed`` markers may be added. Non-list input is handled
    safely as an empty result.
    """
    if type(today) is not date:
        raise ValueError('稍后提醒必须给出未来日期')
    if not isinstance(items, list):
        return []
    latest = _latest_states(states)
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_key = _item_state_key(item)
        state = latest.get(item_key) if item_key is not None else None
        if state is None or not _same_version(state, item):
            result.append(dict(item))
            continue
        action = state.get('action')
        if action == 'done' or action == 'dismiss':
            if include_handled:
                marked = dict(item)
                marked['handled'] = action
                result.append(marked)
            continue
        if action == 'snooze':
            # Only a strictly future snooze hides; anything else (expired or
            # unparseable) brings the reminder back and says it was snoozed.
            until = _parse_date(state.get('until'))
            if until is not None and until > today:
                continue
            resumed = dict(item)
            resumed['resumed'] = True
            result.append(resumed)
            continue
        # Unknown action (unvalidated input) never hides a reminder.
        result.append(dict(item))
    return result


def summarize_states(items) -> dict:
    """Count items by handling marker: {'total','active','handled','snoozed','dismissed'}."""
    result = {'total': 0, 'active': 0, 'handled': 0, 'snoozed': 0, 'dismissed': 0}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        result['total'] += 1
        handled = item.get('handled')
        if handled == 'dismiss':
            result['handled'] += 1
            result['dismissed'] += 1
        elif handled == 'done':
            result['handled'] += 1
        elif item.get('resumed') is True:
            result['snoozed'] += 1
        else:
            result['active'] += 1
    return result
