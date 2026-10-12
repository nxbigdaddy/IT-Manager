"""charts.py 纯函数单测：无 Qt、确定性、覆盖正常 / 空 / 非法输入与边界。"""
from datetime import date
from decimal import Decimal
from unittest import TestCase

from itmanager.charts import (
    BUDGET_FIELDS, RISK_LEVELS, budget_totals, module_totals, percent,
    risk_levels, status_breakdown, top_n, undated_open_tasks, weekly_task_trend,
)
from itmanager.schema import MODULES


def row(identifier='r', module='tasks', archived=0, **data):
    return {'id': str(identifier), 'module': module, 'version': 1,
            'archived': archived, 'data': {'名称': str(identifier), **data}}


def monday_of(value):
    return value - date.resolution * value.weekday()


class ModuleTotalsTests(TestCase):
    def test_counts_by_chinese_title_sorted_desc_then_name(self):
        records = [row(i, 'tasks') for i in range(3)]
        records += [row('p1', 'plans'), row('r1', 'risks'), row('r2', 'risks')]
        result = module_totals(records)
        self.assertEqual(result, [('任务管理', 3), ('风险台账', 2), ('长期规划', 1)])
        self.assertEqual([count for _, count in result], [3, 2, 1])

    def test_illegal_module_and_shape_are_ignored(self):
        records = [row('a'), row('b', 'not_a_module'), {'data': {}}, 'nonsense',
                   {'module': 'tasks'}, row('c', 'people')]
        # 只有合法的 module 键参与统计，data 缺失不影响计数。
        self.assertEqual(module_totals(records), [('任务管理', 2), ('人员与职责', 1)])

    def test_archived_records_excluded_and_empty_input(self):
        records = [row('keep'), row('gone', archived=1), row('gone2', archived=True)]
        self.assertEqual(module_totals(records), [('任务管理', 1)])
        self.assertEqual(module_totals([]), [])
        self.assertEqual(module_totals(None), [])

    def test_all_module_titles_come_from_schema(self):
        records = [row(key, key) for key in MODULES]
        titles = {title for title, _ in module_totals(records)}
        self.assertEqual(titles, {title for title, _ in MODULES.values()})
        for title, count in module_totals(records):
            self.assertEqual(count, 1)
        # 数量相同 -> 名称升序（中文按码位）
        self.assertEqual([t for t, _ in module_totals(records)],
                         sorted(t for t, _ in MODULES.values()))


class StatusBreakdownTests(TestCase):
    def test_groups_counts_and_blank_status(self):
        records = [row(1, 状态='进行中'), row(2, 状态='进行中'), row(3, 状态='已完成'),
                   row(4), row(5, 状态='   '), row(6, 'risks', 状态='进行中')]
        # 同数量按状态文本码位升序（确定性排序，不依赖 locale）。
        self.assertEqual(status_breakdown(records, 'tasks'),
                         [('未填写', 2), ('进行中', 2), ('已完成', 1)])

    def test_unknown_module_and_empty_records(self):
        self.assertEqual(status_breakdown([row(1, 状态='进行中')], 'plans'), [])
        self.assertEqual(status_breakdown([], 'tasks'), [])

    def test_equal_counts_sorted_by_status_text_ascending(self):
        records = [row(1, 状态='阻塞'), row(2, 状态='待确认'), row(3, 状态='进行中')]
        self.assertEqual(status_breakdown(records, 'tasks'),
                         [('待确认', 1), ('进行中', 1), ('阻塞', 1)])
        self.assertEqual([count for _, count in status_breakdown(records, 'tasks')], [1, 1, 1])


class WeeklyTrendTests(TestCase):
    def test_buckets_are_consecutive_mondays_ending_this_week(self):
        today = date(2025, 3, 12)  # 周三
        records = [row(1, 截止日期=(today - date.resolution * 0).isoformat()),
                   row(2, 截止日期=(today - date.resolution * 30).isoformat())]
        trend = weekly_task_trend(records, 8, today)
        self.assertEqual(len(trend), 8)
        labels = [item['label'] for item in trend]
        self.assertEqual(labels, sorted(labels))
        self.assertEqual(labels[-1], (today - date.resolution * today.weekday()).strftime('%m-%d'))

    def test_monday_boundary_today_last_week_and_out_of_range(self):
        today = date(2025, 3, 10)  # 周一
        records = [
            row('this_mon', 截止日期='2025-03-10', 状态='已完成'),
            row('this_sun', 截止日期='2025-03-16'),
            row('last_week', 截止日期='2025-03-09'),          # 上周日 -> 上周
            row('far_past', 截止日期='2024-01-01'),
            row('future', 截止日期='2025-04-01'),             # 超出本周
        ]
        trend = weekly_task_trend(records, 2, today)
        self.assertEqual([item['label'] for item in trend], ['03-03', '03-10'])
        self.assertEqual(trend[-1], {'label': '03-10', 'done': 1, 'open': 1})
        self.assertEqual(trend[0], {'label': '03-03', 'done': 0, 'open': 1})
        self.assertEqual(sum(item['done'] + item['open'] for item in trend), 3)

    def test_undated_tasks_ignored_and_counted_separately(self):
        today = date(2025, 3, 10)
        records = [row('no_date', 状态='进行中'), row('blank', 截止日期=''),
                   row('bad', 截止日期='2025-13-45'), row('ok', 截止日期='2025-03-10'),
                   row('archived', 截止日期='', archived=1),
                   row('other_module', 'risks', 截止日期='')]
        trend = weekly_task_trend(records, 1, today)
        self.assertEqual(sum(item['done'] + item['open'] for item in trend), 1)
        self.assertEqual(undated_open_tasks(records), 3)
        self.assertEqual(undated_open_tasks([]), 0)

    def test_zero_weeks_and_string_today(self):
        today = date(2025, 3, 12)
        records = [row(1, 截止日期='2025-03-11', 状态='已完成')]
        self.assertEqual(weekly_task_trend(records, 0, today), [])
        trend = weekly_task_trend(records, 1, today.isoformat())
        self.assertEqual(trend, [{'label': '03-10', 'done': 1, 'open': 0}])
        self.assertEqual(undated_open_tasks([row(1, 截止日期='2025-03-11')]), 0)

    def test_open_state_variants_are_not_done(self):
        today = date(2025, 3, 12)
        records = [row('done', 截止日期='2025-03-12', 状态='已完成'),
                   row('closed', 截止日期='2025-03-12', 状态='已取消'),
                   row('none', 截止日期='2025-03-12')]
        trend = weekly_task_trend(records, 1, today)
        self.assertEqual(trend[-1], {'label': '03-10', 'done': 1, 'open': 2})


class RiskLevelTests(TestCase):
    def test_fixed_order_and_blank_to_unfilled(self):
        records = [row(1, 'risks', 等级='低'), row(2, 'risks', 等级='高'),
                   row(3, 'risks', 等级='高'), row(4, 'risks', 等级='中'),
                   row(5, 'risks'), row(6, 'risks', 等级='超高'), row(7, 'tasks', 等级='高')]
        self.assertEqual(risk_levels(records), [('高', 2), ('中', 1), ('低', 1), ('未填写', 2)])
        self.assertEqual([level for level, _ in risk_levels(records)], list(RISK_LEVELS))

    def test_empty_input_keeps_zero_buckets(self):
        self.assertEqual(risk_levels([]), [('高', 0), ('中', 0), ('低', 0), ('未填写', 0)])


class BudgetTotalsTests(TestCase):
    def test_sums_real_schema_fields_and_omits_absent_keys(self):
        self.assertEqual(BUDGET_FIELDS, ('预算', '已发生', '已承诺未发生', '预计新增'))
        records = [
            row('b1', 'budget', 预算='1000.50', 已发生='200', 已承诺未发生='50', 预计新增='10'),
            row('b2', 'budget', 预算='999.50', 已发生='100.25'),
        ]
        totals = budget_totals(records)
        self.assertEqual(totals['预算合计'], Decimal('2000.00'))
        self.assertEqual(totals['已发生合计'], Decimal('300.25'))
        self.assertEqual(totals['承诺合计'], Decimal('50'))
        self.assertEqual(totals['预计新增合计'], Decimal('10'))
        self.assertEqual(list(totals), ['预算合计', '已发生合计', '承诺合计', '预计新增合计'])

    def test_illegal_values_skipped_without_raising(self):
        records = [
            row('bad', 'budget', 预算='abc', 已发生='-5', 已承诺未发生='', 预计新增='12,000元'),
            row('ok', 'budget', 预算='100', 已发生=None, 已承诺未发生={}, 预计新增='（300）'),
        ]
        totals = budget_totals(records)
        self.assertEqual(totals, {'预算合计': Decimal('100'), '预计新增合计': Decimal('12000')})
        self.assertEqual(budget_totals([]), {})
        self.assertEqual(budget_totals([row('x', 'tasks', 预算='100')]), {})

    def test_numeric_and_zero_values_are_kept(self):
        records = [row('b', 'budget', 预算=0, 已发生=1500.5, 已承诺未发生='0')]
        totals = budget_totals(records)
        self.assertEqual(totals['预算合计'], Decimal('0'))
        self.assertEqual(totals['承诺合计'], Decimal('0'))
        self.assertEqual(float(totals['已发生合计']), 1500.5)


class PercentTests(TestCase):
    def test_zero_and_negative_total_return_zero(self):
        self.assertEqual(percent(5, 0), 0.0)
        self.assertEqual(percent(5, -1), 0.0)
        self.assertEqual(percent(5, None), 0.0)

    def test_normal_calculation(self):
        self.assertEqual(percent(1, 4), 25.0)
        self.assertEqual(percent(0, 4), 0.0)
        self.assertEqual(percent(Decimal('2.5'), 5), 50.0)


class TopNTests(TestCase):
    def test_truncates_and_folds_tail(self):
        items = [('a', 5), ('b', 4), ('c', 3), ('d', 2)]
        self.assertEqual(top_n(items, 2), [('a', 5), ('b', 4), ('其他', 5)])
        self.assertEqual(top_n(items, 4), items)
        self.assertEqual(top_n(items, 10), items)

    def test_zero_and_empty_input(self):
        self.assertEqual(top_n([('a', 1), ('b', 2)], 0), [('其他', 3)])
        self.assertEqual(top_n([], 3), [])
        self.assertEqual(top_n([], 0), [])


class ModuleTitleContractTests(TestCase):
    def test_monday_of_helper_matches_trend_labels(self):
        today = date(2025, 3, 12)
        expected = monday_of(today).strftime('%m-%d')
        self.assertEqual(weekly_task_trend([], 1, today)[-1]['label'], expected)
