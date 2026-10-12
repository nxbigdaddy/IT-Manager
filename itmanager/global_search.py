"""Pure, bounded global search helpers for business records.

The functions in this module never mutate records and do not perform I/O.  Invalid
public inputs use a small fixed set of messages that is safe to show to users.
"""
import re
from collections import Counter
from typing import Iterable

from .schema import MODULES

SAFE_SEARCH_ERRORS = frozenset({
    '搜索词无效',
    '搜索输入记录无效',
    '搜索模块无效',
    '搜索数量限制无效',
})

_OWNER_FIELDS = (
    '负责人', '项目负责人', '业务负责人', '技术负责人', '责任人', '姓名',
    '执行人', '提供人', '提出人', '决策人', '复核人', '验收人', '主管',
)
_STATUS_FIELDS = ('状态', '核验状态', '结论', 'MFA状态', '财务确认')
_DATE_FIELDS = (
    '截止日期', '日期', '到期日期', '维保到期日期', '复核日期', '演练日期',
    '受理时间', '计划窗口', '开始日期', '采购日期', '恢复时间', '通知截止日期',
)
_TAG = re.compile(r'<[^>]*>')
_SPACE = re.compile(r'\s+')


def tokenize(query) -> list[str]:
    """Case-fold and whitespace-tokenize a non-empty query.

    Continuous Chinese text is deliberately retained as one token: no language
    specific or network tokenizer is used.  Duplicate tokens are removed while
    retaining their first occurrence.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError('搜索词无效')
    result = []
    seen = set()
    for part in query.split():
        folded = part.casefold()
        if not folded or len(folded) > 50:
            raise ValueError('搜索词无效')
        if folded not in seen:
            seen.add(folded)
            result.append(folded)
        if len(result) > 10:
            raise ValueError('搜索词无效')
    if not result:
        raise ValueError('搜索词无效')
    return result


def _modules(value):
    if value is None:
        return None
    if not isinstance(value, (list, tuple, set, frozenset)) or isinstance(value, (str, bytes)):
        raise ValueError('搜索模块无效')
    if not value or any(not isinstance(item, str) or item not in MODULES for item in value):
        raise ValueError('搜索模块无效')
    return set(value)


def _clean_text(value):
    if not isinstance(value, str):
        value = str(value) if value is not None else ''
    return _SPACE.sub(' ', _TAG.sub('', value)).strip()


def _mapped(data, fields):
    values = [_clean_text(data.get(field, '')) for field in fields if data.get(field)]
    return ' / '.join(dict.fromkeys(value for value in values if value))


def _first_mapped(data, fields):
    for field in fields:
        value = _clean_text(data.get(field, ''))
        if value:
            return value[:10] if fields is _DATE_FIELDS else value
    return ''


def _highlight(text, tokens, maximum=180):
    """Return bounded plain text with token matches enclosed by CJK brackets."""
    text = _clean_text(text)
    if not text:
        return ''
    # Python's Unicode IGNORECASE supplies useful highlighting while matching itself
    # remains based on casefold below (including expansions such as ß -> ss).
    pattern = re.compile('|'.join(re.escape(t) for t in sorted(tokens, key=len, reverse=True)), re.IGNORECASE)
    marked = pattern.sub(lambda match: '【' + match.group(0) + '】', text)
    if len(marked) <= maximum:
        return marked
    shortened = marked[:maximum - 1].rstrip()
    # Do not leave an unmatched opening marker at the truncation boundary.
    if shortened.count('【') > shortened.count('】'):
        shortened = shortened.rsplit('【', 1)[0].rstrip()
    return shortened + '…'


def _valid_row(raw):
    if not isinstance(raw, dict):
        return None
    oid, module, data = raw.get('id'), raw.get('module'), raw.get('data')
    if not isinstance(oid, str) or not oid or module not in MODULES or not isinstance(data, dict):
        return None
    if any(not isinstance(key, str) for key in data):
        return None
    return oid, module, data


def search(records, query, modules=None, limit=50, include_archived=False) -> dict:
    """Search well-formed records with AND semantics and deterministic ranking."""
    if not isinstance(records, list):
        raise ValueError('搜索输入记录无效')
    tokens = tokenize(query)
    allowed = _modules(modules)
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('搜索数量限制无效')
    if type(include_archived) is not bool:
        raise ValueError('搜索输入记录无效')

    found = []
    normalized_query = ' '.join(tokens)
    for position, raw in enumerate(records):
        valid = _valid_row(raw)
        if valid is None:
            continue
        oid, module, data = valid
        if allowed is not None and module not in allowed:
            continue
        if not include_archived and bool(raw.get('archived', 0)):
            continue

        title_field = MODULES[module][1][0]
        title = _clean_text(data.get(title_field, ''))
        owner = _first_mapped(data, _OWNER_FIELDS)
        status = _first_mapped(data, _STATUS_FIELDS)
        date = _first_mapped(data, _DATE_FIELDS)
        module_title = MODULES[module][0]
        values = [_clean_text(value) for value in data.values()]
        searchable = [oid, module_title, *values]
        folded = [value.casefold() for value in searchable]
        if any(not any(token in value for value in folded) for token in tokens):
            continue

        score = 0
        title_fold = title.casefold()
        oid_fold = oid.casefold()
        owner_fold = owner.casefold()
        status_fold = status.casefold()
        for token in tokens:
            if title_fold == token:
                score += 100
            elif title_fold.startswith(token):
                score += 80
            elif token in title_fold:
                score += 60
            if oid_fold == token:
                score += 70
            # Other fields score once per token, regardless of how many long fields
            # repeat it.  Module title is searchable but is not a business data field.
            other_values = [value.casefold() for key, value in data.items()
                            if key != title_field and isinstance(value, (str, int, float, bool))]
            if any(token in value for value in other_values):
                score += 15
            if token in owner_fold:
                score += 10
            if token in status_fold:
                score += 10

        snippet_source = title
        for value in [owner, status, date, *values, module_title, oid]:
            if value and any(token in value.casefold() for token in tokens):
                snippet_source = value
                if value != title:
                    break
        result = {
            'id': oid,
            'module': module,
            'module_title': module_title,
            'title': title,
            'owner': owner,
            'status': status,
            'date': date,
            'score': score,
            'snippet': _highlight(snippet_source, tokens),
            'record': dict(raw),
            '_updated': str(raw.get('updated') or ''),
            '_position': position,
        }
        found.append(result)

    # Stable successive sorts express score desc, updated desc, then title/id asc.
    found.sort(key=lambda item: (item['title'].casefold(), item['id'].casefold(), item['_position']))
    found.sort(key=lambda item: item['_updated'], reverse=True)
    found.sort(key=lambda item: item['score'], reverse=True)
    total = len(found)
    results = found[:limit]
    for item in results:
        item.pop('_updated', None)
        item.pop('_position', None)
    return {'query': query, 'tokens': tokens, 'total': total, 'results': results}


def suggestions(records, prefix, limit=10) -> list[str]:
    """Suggest only titles and owners; an empty prefix returns popular values."""
    if not isinstance(records, list):
        raise ValueError('搜索输入记录无效')
    if not isinstance(prefix, str) or len(prefix) > 50:
        raise ValueError('搜索词无效')
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('搜索数量限制无效')
    needle = prefix.strip().casefold()
    counts = Counter()
    display = {}
    for raw in records:
        valid = _valid_row(raw)
        if valid is None or bool(raw.get('archived', 0)):
            continue
        _, module, data = valid
        title = _clean_text(data.get(MODULES[module][1][0], ''))
        owner = _mapped(data, _OWNER_FIELDS)
        for value in (title, owner):
            if not value:
                continue
            key = value.casefold()
            counts[key] += 1
            display.setdefault(key, value)
    keys = [key for key in counts if not needle or key.startswith(needle)]
    keys.sort(key=lambda key: (-counts[key], key))
    return [display[key] for key in keys[:limit]]


def group_counts(results) -> dict:
    """Count result dictionaries by their displayed module title."""
    counts = {}
    if not isinstance(results, list):
        return counts
    for item in results:
        if isinstance(item, dict) and isinstance(item.get('module_title'), str):
            title = item['module_title']
            counts[title] = counts.get(title, 0) + 1
    return counts


__all__ = ['SAFE_SEARCH_ERRORS', 'tokenize', 'search', 'suggestions', 'group_counts']
