"""Offline tests for the AI weekly-summary / minutes-to-tasks pure logic layer.

Every external model call is mocked: no network, no database, no store writes.
The module under test may call ``ai.generate`` at most once per entry point, and
that single call site is ``call_model``.
"""
import copy
import inspect
import json
import re
import unittest
from datetime import date, datetime
from unittest.mock import patch

from itmanager import ai_extras as X
from itmanager.ai import AIError

START, END = '2026-02-16', '2026-02-22'


def rec(oid, module='tasks', updated='2026-02-18T09:00:00+00:00', archived=0, **data):
    return {'id': oid, 'module': module, 'data': data, 'updated': updated,
            'created': '2026-01-01T00:00:00+00:00', 'version': 1, 'archived': archived}


def reply(tasks):
    return json.dumps({'tasks': tasks}, ensure_ascii=False)


class ExtrasBase(unittest.TestCase):
    def assertSafe(self, message, callable_, *args, **kwargs):
        """Assert the exact safe error is raised and that it is a fixed literal."""
        with self.assertRaises(AIError) as caught:
            callable_(*args, **kwargs)
        self.assertIn(str(caught.exception), X.SAFE_EXTRAS_ERRORS)
        self.assertEqual(str(caught.exception), message)

    def assertJsonSafe(self, value):
        text = json.dumps(value, ensure_ascii=False)
        self.assertIsInstance(text, str)

    def assertAllStrings(self, task):
        for key, value in task.items():
            if key == 'warnings':
                self.assertIsInstance(value, list)
                self.assertTrue(all(isinstance(item, str) for item in value))
            else:
                self.assertIsInstance(value, str, key)


class ApiContractTests(ExtrasBase):
    """The public layer is exactly what the server/GUI wiring is allowed to call."""

    def test_public_signatures_are_stable(self):
        expected = {
            'weekly_summary_context': '(records, start, end, owner=\'\')',
            'weekly_summary_prompt': "(context, style='管理摘要')",
            'minutes_to_tasks_prompt': '(minutes_text, people=(), projects=(), today=None)',
            'parse_minutes_tasks': '(text, people=(), projects=(), today=None)',
            'summary_report_text': '(context, result_text)',
            'call_model': '(config, prompt, system=None)',
        }
        for name, signature in expected.items():
            with self.subTest(name=name):
                function = getattr(X, name)
                self.assertTrue(callable(function))
                self.assertEqual(str(inspect.signature(function)), signature)

    def test_module_exports_the_documented_constants(self):
        self.assertEqual(X.MAX_SCOPED_ITEMS, 50)
        self.assertEqual(X.MAX_TASKS, 30)
        self.assertEqual(X.MAX_CONTEXT_CHARS, 100000)
        self.assertEqual(X.STYLES, ('管理摘要',))
        self.assertEqual(X.PRIORITIES, ('高', '中', '低'))


class ErrorAndContextTests(ExtrasBase):
    def test_safe_errors_are_fixed_and_complete(self):
        self.assertIsInstance(X.SAFE_EXTRAS_ERRORS, frozenset)
        self.assertEqual(X.SAFE_EXTRAS_ERRORS, frozenset({
            '周报汇总时间范围无效', '周报汇总输入记录无效', '周报汇总上下文过长',
            '会议纪要内容无效或过长', '会议纪要未识别出可执行任务', '会议纪要任务条数超限',
            '会议纪要解析失败', '会议纪要提示词无效', '周报汇总提示词无效'}))
        self.assertEqual(len(X.SAFE_EXTRAS_ERRORS), 9)

    def test_invalid_period_forms_are_rejected(self):
        for start, end in ((None, END), (START, None), ('2026-2-16', END), (START, '2026/02/22'),
                           ('2026-02-30', END), (START, '20260222'), (123, END), (START, 20260222),
                           ('2026-13-01', END), ('', '')):
            with self.subTest(start=start, end=end):
                self.assertSafe('周报汇总时间范围无效', X.weekly_summary_context, [], start, end)

    def test_reversed_period_is_rejected(self):
        self.assertSafe('周报汇总时间范围无效', X.weekly_summary_context, [], END, START)

    def test_non_list_records_is_rejected(self):
        for value in (None, 'x', {'a': 1}, 3, ('x',)):
            with self.subTest(value=value):
                self.assertSafe('周报汇总输入记录无效', X.weekly_summary_context, value, START, END)

    def test_boundary_days_are_inclusive_and_outside_days_excluded(self):
        records = [rec('A', 名称='起', updated='2026-02-16T00:00:00+00:00'),
                   rec('B', 名称='止', updated='2026-02-22T23:59:59+00:00'),
                   rec('C', 名称='早', updated='2026-02-15T23:59:59+00:00'),
                   rec('D', 名称='晚', updated='2026-02-23T00:00:00+00:00')]
        context = X.weekly_summary_context(records, START, END)
        names = [item['名称'] for item in context['scopes']['tasks']['items']]
        self.assertEqual(names, ['起', '止'])
        self.assertEqual(context['scopes']['tasks']['count'], 2)
        self.assertEqual(context['stats']['included'], 2)
        self.assertEqual(context['period'], {'start': START, 'end': END})
        self.assertJsonSafe(context)

    def test_same_day_period_and_minimal_record_are_supported(self):
        context = X.weekly_summary_context([{'module': 'logs', 'data': {}, 'updated': '2026-02-18'}],
                                           '2026-02-18', '2026-02-18')
        self.assertEqual(context['scopes']['logs']['count'], 1)
        self.assertEqual(context['stats']['included'], 1)

    def test_malformed_records_are_skipped_not_fatal(self):
        records = [None, 'x', {'module': 'tasks'}, {'module': 'nope', 'data': {}},
                   {'module': 'tasks', 'data': {}, 'updated': 'nope'},
                   {'module': 'tasks', 'data': {}, 'updated': None},
                   {'module': 'tasks', 'data': None, 'updated': '2026-02-18'},
                   rec('OK', 名称='正常')]
        context = X.weekly_summary_context(records, START, END)
        self.assertEqual(context['stats']['included'], 1)
        self.assertEqual(context['scopes']['tasks']['items'][0]['名称'], '正常')

    def test_owner_filter_matches_known_owner_fields_only(self):
        records = [rec('A', 名称='甲', 负责人='张三'), rec('B', 名称='乙', 负责人='李四'),
                   rec('C', 名称='丙', 业务负责人='张三'), rec('D', 名称='丁', 提出人='张三'),
                   rec('E', 名称='戊', 下一步='张三负责跟进'), rec('F', 名称='己', 负责人='张三丰')]
        context = X.weekly_summary_context(records, START, END, owner='张三')
        names = [item['名称'] for item in context['scopes']['tasks']['items']]
        self.assertEqual(names, ['甲', '丙', '丁'])
        self.assertEqual(context['owner'], '张三')
        self.assertEqual(X.weekly_summary_context(records, START, END)['stats']['included'], 6)

    def test_archived_records_are_excluded_and_counted(self):
        records = [rec('A', 名称='在', updated='2026-02-18T00:00:00+00:00'),
                   rec('B', 名称='档', updated='2026-02-18T00:00:00+00:00', archived=1),
                   rec('C', 名称='档二', updated='2026-01-01T00:00:00+00:00', archived=1,
                       截止日期='2026-02-01', 状态='进行中')]
        context = X.weekly_summary_context(records, START, END)
        self.assertEqual([i['名称'] for i in context['scopes']['tasks']['items']], ['在'])
        self.assertEqual(context['overdue'], [])
        self.assertEqual(context['stats']['archived'], 2)
        self.assertEqual(context['stats']['included'], 1)

    def test_overdue_detection_requires_open_state_and_passed_due_date(self):
        records = [rec('A', 名称='逾期进行中', 状态='进行中', 截止日期='2026-02-20'),
                   rec('B', 名称='逾期阻塞', 状态='阻塞', 截止日期='2026-02-01'),
                   rec('C', 名称='已完成', 状态='已完成', 截止日期='2026-02-01'),
                   rec('D', 名称='已关闭', 状态='已关闭', 截止日期='2026-02-01'),
                   rec('E', 名称='未到期', 状态='进行中', 截止日期='2026-02-28'),
                   rec('F', 名称='当天到期', 状态='进行中', 截止日期=END),
                   rec('G', 名称='无日期', 状态='进行中'),
                   rec('H', 名称='坏日期', 状态='进行中', 截止日期='2026-2-1')]
        context = X.weekly_summary_context(records, START, END)
        names = sorted(item['名称'] for item in context['overdue'])
        self.assertEqual(names, ['逾期进行中', '逾期阻塞'])
        self.assertEqual(context['stats']['overdue'], 2)
        self.assertEqual(context['overdue'][0]['module'], 'tasks')

    def test_context_trim_caps_each_module_at_fifty_items(self):
        records = [rec('T%03d' % i, 名称='任务%03d' % i, 状态='进行中') for i in range(60)]
        records += [rec('L%03d' % i, module='logs', 名称='记录%03d' % i) for i in range(60)]
        context = X.weekly_summary_context(records, START, END)
        self.assertEqual(context['scopes']['tasks']['count'], 60)
        self.assertEqual(len(context['scopes']['tasks']['items']), 50)
        self.assertEqual(len(context['scopes']['logs']['items']), 50)
        self.assertEqual(context['stats']['included'], 120)
        self.assertJsonSafe(context)

    def long_records(self, count, size=1000):
        return [rec('T%03d' % i, 名称='待确认' * size + str(i), 负责人='待确认' * size + str(i),
                    状态='进行中', 下一步='核实', 阻塞原因='等待反馈') for i in range(count)]

    def test_context_exceeding_limit_after_trimming_is_rejected(self):
        self.assertLessEqual(len(json.dumps(
            X.weekly_summary_context(self.long_records(1), START, END), ensure_ascii=False)), 100000)
        trimmed = X.weekly_summary_context(self.long_records(10), START, END)
        self.assertEqual(trimmed['scopes']['tasks']['count'], 10)
        self.assertEqual(len(trimmed['scopes']['tasks']['items']), 5)
        self.assertLessEqual(len(json.dumps(trimmed, ensure_ascii=False)), 100000)
        oversized = self.long_records(30)
        raw = X._build_context(oversized, [], 0, START, END, '')
        self.assertGreater(len(json.dumps(raw, ensure_ascii=False)), 100000)
        self.assertSafe('周报汇总上下文过长', X.weekly_summary_context, oversized, START, END)

    def test_oversized_context_is_trimmed_until_it_fits(self):
        records = self.long_records(16)
        untrimmed = X._build_context(records, [], 0, START, END, '')
        self.assertGreater(len(json.dumps(untrimmed, ensure_ascii=False)), 100000)
        context = X.weekly_summary_context(records, START, END)
        self.assertEqual(context['scopes']['tasks']['count'], 16)
        self.assertEqual(len(context['scopes']['tasks']['items']), 2)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 100000)
        self.assertIn('"scopes"', X.weekly_summary_prompt(context))
        self.assertSafe('周报汇总提示词无效', X.weekly_summary_prompt,
                        {'period': {}, 'scopes': {}, 'blob': 'x' * 100001})


class PromptTests(ExtrasBase):
    def setUp(self):
        self.context = X.weekly_summary_context([rec('A', 名称='甲', 状态='进行中', 负责人='张三',
                                                     截止日期='2026-02-20', 下一步='联调')], START, END)

    def test_weekly_prompt_contains_sections_and_no_fabrication_rule(self):
        prompt = X.weekly_summary_prompt(self.context)
        self.assertIsInstance(prompt, str)
        for section in ('本期完成', '进行中', '风险与阻塞', '下期计划', '需决策事项'):
            self.assertIn(section, prompt)
        self.assertIn('不得编造', prompt)
        self.assertIn('待确认', prompt)
        self.assertIn('证据缺口', prompt)
        self.assertIn(START, prompt)
        self.assertIn(END, prompt)
        self.assertIn('甲', prompt)
        self.assertIn('管理摘要', prompt)

    def test_weekly_prompt_style_and_context_validation(self):
        for style in ('详细', None, 3, '', '管理摘要 '):
            with self.subTest(style=style):
                self.assertSafe('周报汇总提示词无效', X.weekly_summary_prompt, self.context, style)
        self.assertIn('管理摘要', X.weekly_summary_prompt(self.context, '管理摘要'))
        for bad in (None, 'x', 1, []):
            with self.subTest(context=bad):
                self.assertSafe('周报汇总提示词无效', X.weekly_summary_prompt, bad)
        for bad in (None, 'x', 1, []):
            with self.subTest(context=bad, style='管理摘要'):
                self.assertSafe('周报汇总提示词无效', X.weekly_summary_prompt, bad, '管理摘要')

    def test_minutes_prompt_contract(self):
        prompt = X.minutes_to_tasks_prompt('  周一讨论上线计划，张三负责联调。  ',
                                           people=['张三', '张三', ' 李四 '],
                                           projects=['上线项目'], today=date(2026, 2, 22))
        for token in ('{"tasks"', 'name', 'owner', 'due_date', 'priority', 'next_step', 'acceptance',
                      'YYYY-MM-DD', '2026-02-22', '高', '中', '低', '张三', '李四', '上线项目',
                      '项目名称', '不得编造', '留空', '30'):
            self.assertIn(token, prompt)
        self.assertIn('周一讨论上线计划', prompt)
        self.assertNotIn('  周一讨论', prompt)
        self.assertIn('2026-02-22', X.minutes_to_tasks_prompt('内容', today='2026-02-22'))
        self.assertIn('2026-02-22', X.minutes_to_tasks_prompt('内容', today=datetime(2026, 2, 22, 8, 0)))
        self.assertRegex(X.minutes_to_tasks_prompt('内容'), r'服务端日期：\d{4}-\d{2}-\d{2}')
        for bad in (None, '', '   ', 3, [], {}, b'x', 'x' * 20001):
            with self.subTest(bad=repr(bad)[:20]):
                self.assertSafe('会议纪要内容无效或过长', X.minutes_to_tasks_prompt, bad)
        self.assertTrue(X.minutes_to_tasks_prompt('x' * 20000))


class ParseMinutesTests(ExtrasBase):
    PEOPLE = ('张三', '李四')
    PROJECTS = ('上线项目',)

    def parse(self, payload, **kwargs):
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return X.parse_minutes_tasks(text, **{'people': self.PEOPLE, 'projects': self.PROJECTS,
                                              'today': date(2026, 2, 22), **kwargs})

    def test_valid_payload_returns_str_only_rows_in_model_order(self):
        tasks = self.parse({'tasks': [
            {'name': '任务甲', 'owner': '张三', 'due_date': '2026-03-01', 'priority': '高',
             'next_step': '联调', 'acceptance': '通过验收'},
            {'name': '任务乙'}]})
        self.assertEqual([t['name'] for t in tasks], ['任务甲', '任务乙'])
        self.assertEqual(len(tasks), 2)
        for task in tasks:
            self.assertAllStrings(task)
            self.assertEqual(set(task), {'name', 'owner', 'due_date', 'priority', 'next_step',
                                          'acceptance', '项目名称', 'warnings'})
        self.assertEqual(tasks[0]['owner'], '张三')
        self.assertEqual(tasks[0]['due_date'], '2026-03-01')
        self.assertEqual(tasks[0]['priority'], '高')
        self.assertEqual(tasks[0]['warnings'], [])
        self.assertEqual(tasks[1], {'name': '任务乙', 'owner': '', 'due_date': '', 'priority': '',
                                    'next_step': '', 'acceptance': '', '项目名称': '', 'warnings': []})

    def test_json_code_fence_is_accepted_and_other_fences_rejected(self):
        body = reply([{'name': '任务甲'}])
        for good in ('```json\n' + body + '\n```', '\n```json\n' + body + '\n```\n',
                     '```JSON\n' + body + '\n```'):
            with self.subTest(good=good[:20]):
                self.assertEqual(self.parse(good)[0]['name'], '任务甲')
        for bad in ('```\n' + body + '\n```', '```python\n' + body + '\n```',
                    '前言' + body, body + '说明', '```json\n' + body + '\n'):
            with self.subTest(bad=bad[:20]):
                self.assertSafe('会议纪要解析失败', self.parse, bad)

    def test_missing_or_extra_top_level_keys_are_rejected(self):
        for payload in ({}, {'tasks': [{'name': 'a'}], 'extra': 1}, {'tasks': [], 'notes': []},
                        [{'name': 'a'}], {'tasks': {'name': 'a'}}, {'TASKS': [{'name': 'a'}]},
                        {'tasks': []}, {'tasks': 'x'}, {'tasks': 3}, {'tasks': {}}):
            with self.subTest(payload=payload):
                self.assertSafe('会议纪要解析失败', self.parse, payload)
        for bad in ('null', '3', '"x"', '{', '[', 'NaN', '{"tasks":NaN}',
                    '{"tasks":[{"name":"a"}],"tasks":[{"name":"b"}]}'):
            with self.subTest(bad=bad[:20]):
                self.assertSafe('会议纪要解析失败', self.parse, bad)
        for bad in (None, 3, [], {}, b'x', 'x' * 100001):
            with self.subTest(bad=type(bad).__name__):
                self.assertSafe('会议纪要内容无效或过长', X.parse_minutes_tasks, bad)

    def test_rows_without_usable_name_mean_no_executable_task(self):
        for payload in ({'tasks': [{'owner': '张三'}, {'name': ''}, {'name': '   '}, [], 'x']},
                        {'tasks': [{'name': None}]}, {'tasks': [{'name': 3}]},
                        {'tasks': [{'name': 'x' * 201}]}):
            with self.subTest(payload=payload):
                self.assertSafe('会议纪要未识别出可执行任务', self.parse, payload)

    def test_rows_are_json_safe_and_carry_project_name_for_task_mapping(self):
        tasks = self.parse({'tasks': [{'name': 'a', '项目名称': '上线项目', 'owner': '王五'}]})
        self.assertEqual(tasks[0]['项目名称'], '上线项目')
        self.assertIsInstance(tasks[0]['warnings'], list)
        self.assertJsonSafe(tasks)
        self.assertAllStrings(tasks[0])
        self.assertEqual(set(tasks[0]), set(X.TASK_KEYS) | {'warnings'})

    def test_task_count_limits(self):
        self.assertSafe('会议纪要任务条数超限', self.parse,
                        {'tasks': [{'name': '任务%d' % i} for i in range(31)]})
        tasks = self.parse({'tasks': [{'name': '任务%d' % i} for i in range(30)]})
        self.assertEqual(len(tasks), 30)

    def test_unknown_keys_ignored_and_name_rules(self):
        tasks = self.parse({'tasks': [
            {'name': '保留', 'id': 'T1', 'module': 'tasks', 'extra': {'secret': 1}, 'warnings': ['假']},
            {'name': '', 'owner': '张三'}, {'name': '   '}, {'owner': '张三'},
            {'name': 3}, {'name': 'x' * 201}, {'name': '短', 'next_step': 'y' * 3000}]})
        self.assertEqual([t['name'] for t in tasks], ['保留', '短'])
        self.assertEqual(tasks[0]['warnings'], [])
        self.assertNotIn('id', tasks[0])
        self.assertEqual(len(tasks[1]['next_step']), 2000)

    def test_owner_outside_people_is_cleared_with_warning(self):
        tasks = self.parse({'tasks': [{'name': 'a', 'owner': '王五'}, {'name': 'b', 'owner': '张三'},
                                      {'name': 'c', 'owner': '张三丰'}]})
        self.assertEqual(tasks[0]['owner'], '')
        self.assertTrue(tasks[0]['warnings'])
        self.assertIn('人员', ''.join(tasks[0]['warnings']))
        self.assertEqual(tasks[1]['owner'], '张三')
        self.assertEqual(tasks[1]['warnings'], [])
        self.assertEqual(tasks[2]['owner'], '')
        self.assertAllStrings(tasks[0])
        self.assertEqual(self.parse({'tasks': [{'name': 'd', 'owner': '王五'}]}, people=())[0]['owner'], '')

    def test_invalid_date_and_priority_are_cleared_with_warning(self):
        tasks = self.parse({'tasks': [
            {'name': 'a', 'due_date': '2026-02-30', 'priority': '紧急'},
            {'name': 'b', 'due_date': '2026/02/22', 'priority': '高'},
            {'name': 'c', 'due_date': '2026-02-22', 'priority': '低'},
            {'name': 'd', 'due_date': None, 'priority': None}]})
        self.assertEqual((tasks[0]['due_date'], tasks[0]['priority']), ('', ''))
        self.assertEqual(len(tasks[0]['warnings']), 2)
        self.assertIn('日期', tasks[0]['warnings'][0])
        self.assertIn('优先级', tasks[0]['warnings'][1])
        self.assertEqual((tasks[1]['due_date'], tasks[1]['priority']), ('', '高'))
        self.assertEqual((tasks[2]['due_date'], tasks[2]['priority']), ('2026-02-22', '低'))
        self.assertEqual(tasks[3]['warnings'], [])

    def test_project_name_outside_projects_is_cleared_with_warning(self):
        tasks = self.parse({'tasks': [{'name': 'a', '项目名称': '别的项目'},
                                      {'name': 'b', '项目名称': '上线项目'}]})
        self.assertEqual(tasks[0]['项目名称'], '')
        self.assertTrue(tasks[0]['warnings'])
        self.assertIn('项目', ''.join(tasks[0]['warnings']))
        self.assertEqual(tasks[1]['项目名称'], '上线项目')
        self.assertEqual(tasks[1]['warnings'], [])
        self.assertEqual(self.parse({'tasks': [{'name': 'c', '项目名称': '上线项目'}]},
                                    projects=())[0]['项目名称'], '')

    def test_parse_is_pure_and_does_not_mutate_reference_lists(self):
        people = ['张三']
        projects = ['上线项目']
        payload = {'tasks': [{'name': 'a', 'owner': '王五'}]}
        before = copy.deepcopy(payload)
        X.parse_minutes_tasks(json.dumps(payload, ensure_ascii=False), people, projects, date(2026, 2, 22))
        self.assertEqual(payload, before)
        self.assertEqual(people, ['张三'])
        self.assertEqual(projects, ['上线项目'])


class ReportTextTests(ExtrasBase):
    def test_markdown_contains_period_counts_and_placeholders(self):
        records = [rec('A', 名称='甲', 状态='进行中'), rec('B', module='logs', 名称='乙'),
                   rec('C', 名称='丙', 状态='进行中', 截止日期='2026-02-01')]
        context = X.weekly_summary_context(records, START, END)
        text = X.summary_report_text(context, '## 本期完成\n- 甲：待确认')
        self.assertIsInstance(text, str)
        self.assertIn('#', text)
        self.assertIn(START, text)
        self.assertIn(END, text)
        self.assertIn('任务管理', text)
        self.assertIn('工作记录', text)
        self.assertIn('周期内更新记录：3 条', text)
        self.assertIn('逾期未结束：1 条', text)
        self.assertIn('{{生成时间}}', text)
        self.assertIn('## 本期完成', text)
        self.assertIn('人工', text)

    def test_empty_or_invalid_inputs_are_handled(self):
        for bad in (None, 3, [], {}, b'x'):
            with self.subTest(bad=type(bad).__name__):
                self.assertSafe('周报汇总输入记录无效', X.summary_report_text, {}, bad)
        text = X.summary_report_text(None, '内容')
        self.assertIn('本周期无记录', text)
        self.assertIn('内容', X.summary_report_text({'period': {'start': START, 'end': END}}, '内容'))
        self.assertIn('待确认', X.summary_report_text({}, '   '))


class CallModelTests(ExtrasBase):
    def test_call_model_forwards_arguments_and_normalizes_result(self):
        with patch('itmanager.ai.generate') as mock:
            mock.return_value = {'text': '正文', 'model': 'm1',
                                 'usage': {'total_tokens': 7, 'secret': 'x', 'prompt_tokens': -1}}
            result = X.call_model({'provider': 'deepseek'}, '提示词', system='系统')
            self.assertEqual(mock.call_args.args, ({'provider': 'deepseek'}, '系统', '提示词'))
            self.assertEqual(result, {'text': '正文', 'model': 'm1', 'usage': {'total_tokens': 7}})
            self.assertEqual(X.call_model({'provider': 'deepseek'}, '提示词'),
                             {'text': '正文', 'model': 'm1', 'usage': {'total_tokens': 7}})
            self.assertEqual(mock.call_args.args[1], '')
            mock.return_value = {'text': '正文'}
            self.assertEqual(X.call_model({}, 'p'), {'text': '正文', 'model': '', 'usage': {}})

    def test_call_model_is_the_only_model_call_site_and_errors_propagate(self):
        source = open(X.__file__, encoding='utf-8').read()
        self.assertEqual(len(re.findall(r'ai\.generate\(', source)), 1)
        with patch('itmanager.ai.generate') as mock:
            mock.side_effect = AIError('AI 请求失败，请检查网络和配置后重试。')
            with self.assertRaises(AIError) as caught:
                X.call_model({}, 'p')
            self.assertEqual(str(caught.exception), 'AI 请求失败，请检查网络和配置后重试。')
            mock.side_effect = None
            mock.return_value = 'not a dict'
            self.assertSafe('周报汇总输入记录无效', X.call_model, {}, 'p')
            for bad in (None, 'p', 3):
                with self.subTest(bad=bad):
                    self.assertSafe('周报汇总输入记录无效', X.call_model, bad, 'p')
        with patch('itmanager.ai.generate') as mock:
            mock.return_value = {'text': None}
            self.assertSafe('周报汇总输入记录无效', X.call_model, {}, 'p')

    def test_entry_points_do_not_call_the_model(self):
        with patch('itmanager.ai.generate') as mock:
            records = [rec('A', 名称='甲', 状态='进行中', 截止日期='2026-02-20')]
            context = X.weekly_summary_context(records, START, END)
            X.weekly_summary_prompt(context)
            X.minutes_to_tasks_prompt('会议内容', ['张三'], [], date(2026, 2, 22))
            X.parse_minutes_tasks(reply([{'name': 'a'}]), ['张三'])
            X.summary_report_text(context, '正文')
            mock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
