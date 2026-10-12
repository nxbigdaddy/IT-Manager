import copy
import inspect

import pytest

from itmanager.global_search import (
    SAFE_SEARCH_ERRORS, group_counts, search, suggestions, tokenize,
)


def row(oid='TASKS-1', module='tasks', updated='2025-01-01T00:00:00+00:00', archived=0, **data):
    defaults = {'名称': '部署系统', '负责人': 'Alice', '状态': '进行中', '截止日期': '2025-03-01'}
    defaults.update(data)
    return {'id': oid, 'module': module, 'data': defaults, 'updated': updated, 'archived': archived, 'version': 1}


def test_public_signatures():
    assert str(inspect.signature(tokenize)) == '(query) -> list[str]'
    assert str(inspect.signature(search)).startswith('(records, query, modules=None, limit=50, include_archived=False)')
    assert str(inspect.signature(suggestions)).startswith('(records, prefix, limit=10)')


def test_safe_error_set_is_exact_and_fixed():
    assert SAFE_SEARCH_ERRORS == frozenset({'搜索词无效', '搜索输入记录无效', '搜索模块无效', '搜索数量限制无效'})


def test_tokenize_chinese_continuous_and_spaces():
    assert tokenize(' 信息安全  风险 ') == ['信息安全', '风险']


def test_tokenize_unicode_casefold_and_deduplicates():
    assert tokenize('Straße STRASSE Alpha alpha') == ['strasse', 'alpha']


@pytest.mark.parametrize('bad', [None, 1, '', '  '])
def test_tokenize_rejects_empty_and_non_string(bad):
    with pytest.raises(ValueError, match='^搜索词无效$'):
        tokenize(bad)


def test_tokenize_limits_tokens_and_token_length():
    for value in (' '.join(str(i) for i in range(11)), 'x' * 51):
        with pytest.raises(ValueError, match='^搜索词无效$'):
            tokenize(value)


def test_search_chinese_and_module_chinese_name():
    records = [row()]
    assert search(records, '部署')['results'][0]['id'] == 'TASKS-1'
    assert search(records, '任务管理')['total'] == 1


def test_search_english_casefold():
    assert search([row(负责人='Straße')], 'STRASSE')['total'] == 1


def test_search_multiple_tokens_are_and_not_or():
    records = [row(), row('TASKS-2', 名称='部署网络', 负责人='Bob')]
    assert [x['id'] for x in search(records, '部署 Alice')['results']] == ['TASKS-1']
    assert search(records, '部署 Nobody')['total'] == 0


def test_title_score_exact_prefix_contains():
    records = [row('1', 名称='系统'), row('2', 名称='系统升级'), row('3', 名称='核心系统升级')]
    results = search(records, '系统')['results']
    assert [(x['id'], x['score']) for x in results] == [('1', 100), ('2', 80), ('3', 60)]


def test_id_exact_and_other_field_scores():
    exact = search([row('TASKS-1')], 'tasks-1')['results'][0]
    other = search([row(备注='季度目标')], '季度')['results'][0]
    assert exact['score'] == 70
    assert other['score'] == 15


def test_owner_and_status_receive_bonus():
    owner = search([row()], 'alice')['results'][0]
    status = search([row()], '进行中')['results'][0]
    assert owner['score'] == 25
    assert status['score'] == 25


def test_owner_status_date_mapping():
    result = search([row(项目负责人='Bob', 核验状态='已核验', 到期日期='2025-04-02')], '部署')['results'][0]
    assert result['owner'] == 'Alice'
    assert result['status'] == '进行中'
    assert result['date'] == '2025-03-01'


def test_archived_excluded_by_default_and_optional():
    records = [row(archived=1)]
    assert search(records, '部署')['total'] == 0
    assert search(records, '部署', include_archived=True)['total'] == 1


def test_module_filter_and_whitelist_validation():
    records = [row(), row('PROJECTS-1', module='projects', 名称='部署项目')]
    assert [x['module'] for x in search(records, '部署', modules=['projects'])['results']] == ['projects']
    for bad in ['tasks', [], ['unknown'], [1]]:
        with pytest.raises(ValueError, match='^搜索模块无效$'):
            search(records, '部署', modules=bad)


def test_records_and_limit_validation():
    with pytest.raises(ValueError, match='^搜索输入记录无效$'):
        search({}, 'x')
    for value in (0, 201, True, 1.5):
        with pytest.raises(ValueError, match='^搜索数量限制无效$'):
            search([], 'x', limit=value)


def test_limit_is_applied_after_total():
    result = search([row(str(i)) for i in range(3)], '部署', limit=2)
    assert result['total'] == 3 and len(result['results']) == 2


def test_sort_score_then_updated_then_title_id():
    records = [
        row('B', updated='2025-02-01', 名称='系统乙'),
        row('C', updated='2025-02-01', 名称='系统甲'),
        row('A', updated='2025-01-01', 名称='系统甲'),
    ]
    assert [x['id'] for x in search(records, '系统')['results']] == ['B', 'C', 'A']


def test_snippet_is_plain_bounded_and_highlighted():
    value = '<script>alert(1)</script> 系统 ' + '很长' * 200
    snippet = search([row(备注=value)], '系统')['results'][0]['snippet']
    assert len(snippet) <= 180
    assert '<' not in snippet and '>' not in snippet and '【系统】' in snippet


def test_result_contract_and_shallow_record_copy():
    source = row()
    result = search([source], '部署')
    assert set(result) == {'query', 'tokens', 'total', 'results'}
    hit = result['results'][0]
    assert set(hit) == {'id', 'module', 'module_title', 'title', 'owner', 'status', 'date', 'score', 'snippet', 'record'}
    assert hit['record'] == source and hit['record'] is not source


def test_suggestions_prefix_casefold_and_deduplicate():
    records = [row('1', 名称='Alpha项目', 负责人='Alice'), row('2', 名称='alpha项目', 负责人='ALICE'), row('3', 名称='Beta', 负责人='Bob')]
    assert suggestions(records, 'AL') == ['Alice', 'Alpha项目']


def test_empty_suggestions_are_popular_then_dictionary_order():
    records = [row('1', 名称='Zulu', 负责人='Bob'), row('2', 名称='Zulu', 负责人='Alice'), row('3', 名称='Alpha', 负责人='Bob')]
    assert suggestions(records, '')[:2] == ['Bob', 'Zulu']
    assert suggestions(records, '')[2:] == ['Alice', 'Alpha']


def test_suggestions_do_not_leak_evidence_or_other_full_text():
    records = [row(名称='安全任务', 负责人='王工', 完成证据='绝密证据全文 token-secret')]
    values = suggestions(records, '')
    assert values == ['安全任务', '王工']
    assert all('绝密' not in value and 'secret' not in value for value in values)


def test_suggestion_validation_and_limit():
    with pytest.raises(ValueError, match='^搜索输入记录无效$'):
        suggestions(None, '')
    with pytest.raises(ValueError, match='^搜索词无效$'):
        suggestions([], None)
    with pytest.raises(ValueError, match='^搜索数量限制无效$'):
        suggestions([], '', 0)
    assert len(suggestions([row('1'), row('2', 名称='另一项')], '', 1)) == 1


def test_group_counts():
    results = search([row(), row('PROJECTS-1', module='projects', 名称='部署项目')], '部署')['results']
    assert group_counts(results) == {'任务管理': 1, '项目管理': 1}


def test_input_is_unchanged():
    records = [row(备注='<b>系统</b>')]
    original = copy.deepcopy(records)
    search(records, '系统')
    suggestions(records, '')
    assert records == original


def test_malformed_records_are_skipped():
    records = [None, {}, {'id': 'X', 'module': 'bad', 'data': {}}, {'id': 'Y', 'module': 'tasks', 'data': []}, row()]
    assert [x['id'] for x in search(records, '部署')['results']] == ['TASKS-1']


def test_duplicate_ids_have_stable_input_order():
    records = [row('SAME', 名称='系统甲'), row('SAME', 名称='系统甲')]
    result = search(records, '系统')['results']
    assert len(result) == 2
    assert result[0]['record'] is not result[1]['record']
    assert result[0]['record']['data'] is records[0]['data']
