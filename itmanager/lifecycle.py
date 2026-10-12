"""Read-only lifecycle chains over stored records: no writes, no IO, no network.

This module only groups and links the records an operator already entered so a
human can review the path 事件 → 根因 → 变更 → 验证 → 复盘. It never decides that
an incident is resolved, never scores anyone and never invents facts: a gap is
reported only when the corresponding record is actually absent or a reference
cannot be followed.

Terminology
-----------
* 记录 record — exactly the shape ``store.list_records()`` returns, i.e.
  ``{'id': str, 'module': str, 'data': dict, 'archived': bool, ...}``. The
  editable field names come from :mod:`itmanager.schema`.
* 链条 chain — the set of records reachable from one starting record through the
  cross-module reference fields listed in :data:`LINK_FIELDS`.
* 阶段 stage — the lifecycle step a module belongs to.

Stage mapping (module → stage), the single source of truth used by every
function here:

===========  =====  =========================================================
stage        label  modules
===========  =====  =========================================================
``event``    事件   incidents
``cause``    根因   risks, decisions, requests, systems, assets, access,
                    collection, people, contracts, budget
``change``   变更   changes
``verify``   验证   recovery, metrics, tasks, reviews, projects, plans
``review``   复盘   logs
===========  =====  =========================================================

Both link directions are followed: A referencing B (``A.关联ID == B.id``) and
B referencing A (some other record's reference field holding this id). Records
already archived are excluded from chains entirely. The same record is reported
exactly once (deduplicated by ``id``), and a whole chain is capped by
``max_depth`` plus :data:`MAX_DEPTH_LIMIT` so cyclic or self-referencing data
can never loop forever.
"""
from __future__ import annotations

from datetime import date
from typing import Dict, List

from .schema import MODULES

# Fixed user-facing errors; never interpolate untrusted values into exceptions.
SAFE_LIFECYCLE_ERRORS = frozenset({
    '生命周期输入记录无效', '生命周期链条类型无效', '生命周期查询记录不存在',
    '生命周期关联字段不符合模块定义', '生命周期链条过长',
})

#: Ordered lifecycle steps. ``incidents`` opens the chain and ``logs`` closes it.
STAGES = ('event', 'cause', 'change', 'verify', 'review')

#: Chinese labels for :data:`STAGES`, for read-only display.
STAGE_LABELS = {'event': '事件', 'cause': '根因', 'change': '变更',
                'verify': '验证', 'review': '复盘'}

#: Module → lifecycle stage. Every module of :data:`itmanager.schema.MODULES`
#: appears exactly once; see the module docstring for the reasoning.
STAGE_MODULES = {
    'incidents': 'event',
    'risks': 'cause', 'decisions': 'cause', 'requests': 'cause',
    'systems': 'cause', 'assets': 'cause', 'access': 'cause',
    'collection': 'cause', 'people': 'cause', 'contracts': 'cause',
    'budget': 'cause',
    'changes': 'change',
    'recovery': 'verify', 'metrics': 'verify', 'tasks': 'verify',
    'reviews': 'verify', 'projects': 'verify', 'plans': 'verify',
    'logs': 'review',
}
# Fallback keeps the module importable and total even if schema adds a module.
_FALLBACK_STAGE = 'cause'

#: Cross-module reference fields actually present in the schema, mapped to the
#: only modules a valid value may point at. ``关联ID``/``来源ID`` have no single
#: target module in the schema, so they accept any known module.
LINK_FIELDS = {
    '关联ID': tuple(MODULES),
    '项目ID': ('projects',),
    '父任务ID': ('tasks',),
    '整改任务ID': ('tasks',),
    '后续任务ID': ('tasks',),
    '系统ID': ('systems',),
    '人员ID': ('people',),
    '来源ID': tuple(MODULES),
    '规划ID': ('plans',),
}

#: Hard ceiling for a requested chain depth (``max_depth`` 1..MAX_DEPTH_LIMIT).
MAX_DEPTH_LIMIT = 4
DEFAULT_MAX_DEPTH = 4

#: Readable bucket names of :func:`chain`'s ``related`` mapping.
RELATED_BUCKETS = {'tasks': ('tasks',), 'logs': ('logs',), 'risks': ('risks',),
                   'changes': ('changes',)}

# Fixed gap texts; deliberately wording-neutral so nothing reads as a verdict.
_GAP_NO_CAUSE = '事件记录未关联根因/风险分析'
_GAP_NO_CHANGE = '缺少变更记录'
_GAP_NO_VERIFY = '缺少验证证据或恢复演练记录'
_GAP_NO_REVIEW = '缺少复盘工作记录'
_GAP_EVENT_CAUSE_FIELD = '事件记录的根因字段为空，根因尚未记录'
_GAP_NO_LINKED_TASKS = '缺少与事件或风险关联的后续任务/整改任务'
_GAP_NO_RECOVERY = '缺少备份恢复演练记录'
_GAP_NO_LOG = '工作记录中未见与事件或风险关联的记录'
_GAP_DANGLING = '存在指向不存在记录或已归档记录的关联ID，链条可能不完整'
_GAP_TRUNCATED = '链条超出最大层数已截断，更深层关联未展开'

#: Which stage's gap text applies when a stage has no node at all.
_STAGE_GAPS = {'event': '未找到运维事件记录', 'cause': _GAP_NO_CAUSE,
               'change': _GAP_NO_CHANGE, 'verify': _GAP_NO_VERIFY,
               'review': _GAP_NO_REVIEW}

#: Date field preference per module, most specific first.
_DATE_FIELDS = {
    'plans': ('截止日期', '开始日期'), 'projects': ('截止日期', '开始日期'),
    'tasks': ('截止日期', '开始日期'), 'people': (),
    'reviews': ('考核周期',), 'logs': ('日期',), 'collection': ('截止日期',),
    'systems': (), 'assets': ('维保到期日期', '采购日期'),
    'access': ('复核日期',), 'risks': ('复核日期', '截止日期'),
    'requests': (), 'incidents': ('恢复时间', '受理时间'),
    'changes': ('计划窗口',), 'contracts': ('到期日期',),
    'budget': (), 'recovery': ('演练日期',), 'decisions': ('截止日期',),
    'metrics': ('周期',),
}

# Legacy module keys tolerated in stored/imported data so validation stays honest
# about what a database may already contain.
_LEGACY_MODULES = {'inventory': 'collection', 'evidence': 'collection',
                   'defects': 'risks', 'vendors': 'contracts'}


# --------------------------------------------------------------------------- #
# small pure helpers
# --------------------------------------------------------------------------- #
def _text(value) -> str:
    """Return a stripped string for str input, otherwise an empty string."""
    return value.strip() if isinstance(value, str) else ''


def _parse_date(value):
    """Parse the leading YYYY-MM-DD of a value; return None when unusable."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _is_archived(row) -> bool:
    """True only for the explicit archive flag; absent means active."""
    return isinstance(row, dict) and bool(row.get('archived'))


def _stage_of(module) -> str:
    """Map a module key to its lifecycle stage (fallback: ``cause``)."""
    if not isinstance(module, str):
        return _FALLBACK_STAGE
    return STAGE_MODULES.get(module) or (STAGE_MODULES.get(_LEGACY_MODULES.get(module))
                                         or _FALLBACK_STAGE)


def _module_known(module) -> bool:
    return isinstance(module, str) and (module in MODULES or module in _LEGACY_MODULES)


def title_of(module, data) -> str:
    """Safely read a record's display title; unknown module or data yields ''.

    The title is the first declared field of the module (see
    :data:`itmanager.schema.MODULES`), never free text other than that field.
    """
    if not _module_known(module) or not isinstance(data, dict):
        return ''
    fields = MODULES.get(module) or MODULES[_LEGACY_MODULES[module]]
    value = data.get(fields[1][0])
    return value.strip() if isinstance(value, str) else ''


def _row_title(row) -> str:
    return title_of(row.get('module'), row.get('data') or {})


def _row_status(row) -> str:
    data = row.get('data')
    return _text(data.get('状态')) if isinstance(data, dict) else ''


def _row_date(row):
    """First usable date of the record: module date fields, then updated/created."""
    module, data = row.get('module'), row.get('data')
    data = data if isinstance(data, dict) else {}
    for field in _DATE_FIELDS.get(module, ()):
        parsed = _parse_date(data.get(field))
        if parsed is not None:
            return parsed.isoformat()
    for key in ('updated', 'created'):
        parsed = _parse_date(row.get(key))
        if parsed is not None:
            return parsed.isoformat()
    return ''


def _node(row, depth, via) -> dict:
    return {'id': row['id'], 'module': row['module'], 'title': _row_title(row),
            'stage': _stage_of(row['module']), 'status': _row_status(row),
            'date': _row_date(row), 'depth': depth, 'via': via}


# --------------------------------------------------------------------------- #
# validation and indexing
# --------------------------------------------------------------------------- #
def _clean_id(value) -> str:
    """Return a usable record id or raise the fixed input error."""
    cleaned = _text(value)
    if not cleaned:
        raise ValueError('生命周期输入记录无效')
    return cleaned


def _valid_records(records) -> list:
    if not isinstance(records, list):
        raise ValueError('生命周期输入记录无效')
    return records


def _validate_max_depth(value) -> int:
    if type(value) is not int or not 1 <= value <= MAX_DEPTH_LIMIT:
        raise ValueError('生命周期链条过长')
    return value


def _index_rows(records) -> Dict[str, dict]:
    """Index usable, non-archived records by id; silently skip malformed rows.

    A normalised copy is stored so callers can pass store rows, plain dicts or
    test fixtures interchangeably. Rows with an unknown module are ignored here
    instead of aborting the whole view; only the explicitly queried record must
    be schema-valid, otherwise :func:`chain` raises
    ``'生命周期关联字段不符合模块定义'``.
    """
    index: Dict[str, dict] = {}
    for row in records:
        if not isinstance(row, dict):
            continue
        record_id, module, data = row.get('id'), row.get('module'), row.get('data')
        if not isinstance(record_id, str) or not record_id.strip():
            continue
        if not _module_known(module) or not isinstance(data, dict):
            continue
        if _is_archived(row) or record_id in index:
            continue
        index[record_id] = {'id': record_id, 'module': module, 'data': data,
                            'updated': row.get('updated'), 'created': row.get('created')}
    return index


def _validate_root(row) -> None:
    if not isinstance(row, dict):
        raise ValueError('生命周期输入记录无效')
    if not _module_known(row.get('module')):
        raise ValueError('生命周期关联字段不符合模块定义')
    if not isinstance(row.get('data'), dict):
        raise ValueError('生命周期输入记录无效')


# --------------------------------------------------------------------------- #
# traversal
# --------------------------------------------------------------------------- #
def _out_targets(index, row):
    """Yield ``(field, target_row)`` for every outgoing reference of ``row``.

    A reference whose id is absent from the index (unknown or archived) is not
    followed here; :func:`_dangling_reference` reports it as a gap instead of
    letting it disappear silently.
    """
    data = row.get('data') or {}
    for field in sorted(LINK_FIELDS):
        value = _text(data.get(field))
        if not value:
            continue
        target = index.get(value)
        if target is None or target['id'] == row['id']:
            continue
        yield field, target


def _in_targets(index, row, ordered_ids, skip_ids):
    """Yield ``(field, target_row)`` for rows whose reference points at ``row``.

    ``LINK_FIELDS`` declares the module each reference field is allowed to point
    at, so a row is only accepted when the *queried* row's module is one of the
    allowed targets of that field — this is where "关联字段不符合模块定义" is
    enforced during traversal. ``skip_ids`` are rows already emitted for this
    step in the outgoing direction, so a record linked both ways is described by
    its forward link instead of a spurious back-link to itself.
    """
    row_id, module = row['id'], row['module']
    for other_id in ordered_ids:
        other = index.get(other_id)
        if other is None or other['id'] == row_id or other['id'] in skip_ids:
            continue
        data = other.get('data') or {}
        for field in sorted(LINK_FIELDS):
            if _text(data.get(field)) != row_id:
                continue
            if module not in LINK_FIELDS[field]:
                continue
            yield field, other


def _neighbour_rows(index, rows, ordered_ids):
    """All immediate neighbours of ``rows`` as ``(target, via)`` pairs.

    Both link directions are included. Pairs are collected and de-duplicated
    within the step before the caller assigns anything, so a record reached by
    two equally short paths is described by the same, order-independent path no
    matter how the input list happened to be ordered.
    """
    candidates = {}
    for row in rows:
        for field, target in _out_targets(index, row):
            candidates.setdefault(target['id'], (target, {'from': row['id'], 'field': field,
                                                          'direction': 'out'}))
    for row in rows:
        for field, target in _in_targets(index, row, ordered_ids, candidates):
            if target['id'] in candidates:
                continue
            candidates[target['id']] = (target, {'from': row['id'], 'field': field,
                                                 'direction': 'in'})
    return [candidates[key] for key in sorted(candidates)]


def _build_chain(root, index, max_depth):
    """Breadth-first walk; return ``(nodes, truncated)``.

    Depth is the hop count from the starting record (root = 0), and a node is
    always reported at its shortest distance. The frontier is expanded once more
    than ``max_depth`` only to learn whether deeper links exist, which is what
    the truncation gap reports.
    """
    nodes = [_node(root, 0, None)]
    seen = {root['id']}
    ordered_ids = tuple(index)
    level, depth, truncated = [root], 0, False
    while level and depth < max_depth:
        depth += 1
        next_level = []
        for target, via in _neighbour_rows(index, level, ordered_ids):
            if target['id'] in seen:
                continue
            seen.add(target['id'])
            nodes.append(_node(target, depth, via))
            next_level.append(target)
        level = next_level
    if level:
        truncated = bool(_neighbour_rows(index, level, ordered_ids))
    return nodes, truncated


def _dangling_reference(index, rows) -> bool:
    """True when a chain record references an id missing from the index.

    Because the index holds every usable non-archived record, a missing id means
    the target does not exist or is archived; either way the link cannot be
    followed and the chain may be incomplete.
    """
    for row in rows:
        data = row.get('data') or {}
        for field in sorted(LINK_FIELDS):
            value = _text(data.get(field))
            if value and value != row['id'] and value not in index:
                return True
    return False


def _has_module(stage_modules, wanted) -> bool:
    return bool(stage_modules & set(wanted))


def _linked_log(index, rows) -> bool:
    """True when a 工作记录 tracks a record that the chain's 事件/风险 lead to.

    The anchor set is every 事件/风险 of the chain; walking outwards from them
    (remaining inside the chain) collects the 变更/任务/演练 records they lead
    to. A log counts as linked when its own ``关联ID`` resolves to one of those;
    pointing back at the event or risk itself is deliberately not enough, so this
    gap stays a statement about missing follow-up evidence rather than a guess.
    """
    chain_ids = {row['id'] for row in rows}
    anchors = {row['id'] for row in rows if row['module'] in ('incidents', 'risks')}
    ordered_ids = tuple(index)
    targets = set()
    for row in rows:
        if row['id'] not in anchors:
            continue
        seen, frontier = {row['id']}, [row]
        while frontier:
            following = []
            for current in frontier:
                neighbours = list(_out_targets(index, current))
                neighbours.extend(_in_targets(index, current, ordered_ids, ()))
                for _, target in neighbours:
                    if target['id'] in seen or target['id'] not in chain_ids:
                        continue
                    seen.add(target['id'])
                    following.append(target)
            for reached_row in following:
                if reached_row['module'] != 'logs' and reached_row['id'] not in anchors:
                    targets.add(reached_row['id'])
            frontier = following
    if not targets:
        return False
    for row in rows:
        if row['module'] != 'logs':
            continue
        if _text((row.get('data') or {}).get('关联ID')) in targets:
            return True
    return False


def _gaps_for(index, rows, truncated) -> List[str]:
    """Human-readable gaps, each one only when the underlying record is absent.

    A stage that has no node at all produces exactly one stage-level gap; the
    finer-grained texts are added only when they carry information the
    stage-level text does not, so the same absence is never reported twice.
    """
    gaps: List[str] = []
    stages = {_stage_of(row['module']) for row in rows}
    for stage in STAGES:
        if stage not in stages:
            gaps.append(_STAGE_GAPS[stage])
    modules = {row['module'] for row in rows}
    if 'event' in stages:
        incidents = [row for row in rows if _stage_of(row['module']) == 'event']
        if incidents and all(not _text((row.get('data') or {}).get('根因')) for row in incidents):
            gaps.append(_GAP_EVENT_CAUSE_FIELD)
    if 'verify' in stages and not _has_module(modules, ('tasks',)):
        # Verification exists but no task carries the work forward.
        gaps.append(_GAP_NO_LINKED_TASKS)
    if 'verify' in stages and not _has_module(modules, ('recovery',)):
        gaps.append(_GAP_NO_RECOVERY)
    if 'review' in stages and not _linked_log(index, rows):
        gaps.append(_GAP_NO_LOG)
    if 'change' in stages and _dangling_reference(index, rows):
        # A link that cannot be followed may hide records; the likely module of an
        # unreachable record is not knowable, so the wording stays generic.
        gaps.append(_GAP_DANGLING)
    if truncated:
        gaps.append(_GAP_TRUNCATED)
    return gaps


def _related(rows) -> dict:
    """Bucket the chain by the four module views the UI shows, id-sorted."""
    out = {}
    for bucket, modules in RELATED_BUCKETS.items():
        out[bucket] = [{'id': row['id'], 'module': row['module'], 'title': _row_title(row)}
                       for row in rows if row['module'] in modules]
    return out


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def chain(records, record_id, max_depth=DEFAULT_MAX_DEPTH) -> dict:
    """Build the read-only lifecycle chain that starts at ``record_id``.

    ``records`` is the list returned by ``store.list_records()`` (plain dicts are
    accepted). Starting from the requested record, every record linked through
    :data:`LINK_FIELDS` is discovered level by level in both directions
    (this record references another, or another references this one), up to
    ``max_depth`` hops; deeper links are cut off and reported in ``gaps``
    instead of raising. Each record appears once, deduplicated by ``id``;
    archived records never participate.

    Returns ``{'root': {'id','module','title'},
    'nodes': [{'id','module','title','stage','status','date','depth','via'}...],
    'gaps': [str], 'related': {'tasks': [...], 'logs': [...], 'risks': [...],
    'changes': [...]}}`` where ``via`` is ``None`` for the root and otherwise
    ``{'from': id, 'field': 关联字段, 'direction': 'out'|'in'}``. ``gaps`` lists
    only records that are genuinely missing; nothing here asserts that an
    incident is understood or resolved.

    Raises :class:`ValueError` (message from :data:`SAFE_LIFECYCLE_ERRORS`) when
    ``records`` is not a list, when ``record_id`` is missing/blank/unknown, when
    the starting record is not a valid schema record, or when ``max_depth`` is
    not an int within ``1..MAX_DEPTH_LIMIT``.
    """
    rows_in = _valid_records(records)
    wanted = _clean_id(record_id)
    depth = _validate_max_depth(max_depth)
    index = _index_rows(rows_in)
    if wanted not in index:
        # Distinguish "not stored at all" from "stored under a module that is not
        # part of the schema"; both are fixed messages, never interpolated.
        for row in rows_in:
            if isinstance(row, dict) and _text(row.get('id')) == wanted and not _is_archived(row):
                raise ValueError('生命周期关联字段不符合模块定义')
        raise ValueError('生命周期查询记录不存在')
    root = index[wanted]
    _validate_root(root)
    nodes, truncated = _build_chain(root, index, depth)
    chain_rows = [index[node['id']] for node in nodes]
    return {'root': {'id': root['id'], 'module': root['module'],
                     'title': _row_title(root)},
            'nodes': nodes,
            'gaps': _gaps_for(index, chain_rows, truncated),
            'related': _related(chain_rows)}


def summarize_chain(result) -> dict:
    """Count a :func:`chain` result by stage and report record *coverage* only.

    ``complete`` is True exactly when the ``event``, ``change`` and ``verify``
    stages each contain at least one node. A missing 根因 or 复盘 entry only
    raises the ``gaps`` count; it never flips ``complete``. ``complete``
    therefore means "the chain has records in those three bookkeeping stages",
    not that a problem was analysed, fixed or verified, and never that an
    incident is resolved.

    Malformed input is tolerated and summarised as empty rather than raising.
    """
    by_stage = {stage: 0 for stage in STAGES}
    nodes = result.get('nodes') if isinstance(result, dict) else None
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        stage = node.get('stage')
        if stage in by_stage:
            by_stage[stage] += 1
    gaps = result.get('gaps') if isinstance(result, dict) else None
    gap_count = len(gaps) if isinstance(gaps, list) else 0
    complete = all(by_stage[stage] > 0 for stage in ('event', 'change', 'verify'))
    return {'total': sum(by_stage.values()), 'by_stage': by_stage,
            'gaps': gap_count, 'complete': complete}


def stage_progress(records) -> List[dict]:
    """Count the whole library by lifecycle stage, for a read-only dashboard.

    Returns one ``{'stage','label','count'}`` entry per entry of :data:`STAGES`,
    always in lifecycle order and always including zero counts. Archived records
    are excluded, matching :func:`chain`.
    """
    rows_in = _valid_records(records)
    counts = {stage: 0 for stage in STAGES}
    for _, row in sorted(_index_rows(rows_in).items()):
        counts[_stage_of(row['module'])] += 1
    return [{'stage': stage, 'label': STAGE_LABELS[stage], 'count': counts[stage]}
            for stage in STAGES]
