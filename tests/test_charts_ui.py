"""charts_ui.ChartsMixin 离屏测试：最小 stub 宿主 + 真实绘制（grab 成 QPixmap）。"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date, timedelta
from unittest import TestCase

from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPushButton, QVBoxLayout, QWidget

from itmanager.charts import BarChart, GroupedBarChart
from itmanager.charts_ui import ChartsMixin


def row(identifier, module='tasks', **data):
    return {'id': str(identifier), 'module': module, 'version': 1, 'archived': 0,
            'data': {'名称': str(identifier), **data}}


def sample_records(today=None):
    """相对“今天”构造样本，保证趋势图落在包含本周的 8 周窗口内。"""
    today = today or date.today()
    iso = lambda days: (today + timedelta(days=days)).isoformat()
    return [
        row('t1', 截止日期=iso(0), 状态='已完成', 负责人='张三'),
        row('t2', 截止日期=iso(-1), 状态='进行中'),
        row('t3', 截止日期=iso(-9), 状态='阻塞'),
        row('t4', 状态='进行中'),
        row('p1', 'plans', 状态='进行中'),
        row('r1', 'risks', 等级='高'),
        row('r2', 'risks', 等级='中'),
        row('r3', 'risks'),
        row('b1', 'budget', 预算='1000', 已发生='250.5', 已承诺未发生='100', 预计新增='无效值'),
    ]


class Host(ChartsMixin):
    """最小宿主：只提供 records / page_layout / refresh。"""

    def __init__(self, records=(), with_refresh=True, raw_records=False):
        self.records = records if raw_records else list(records)
        self.refreshes = 0
        self.page = QWidget()
        self.page_layout = QVBoxLayout(self.page)
        if with_refresh:
            self.refresh = self._refresh

    def _refresh(self):
        self.refreshes += 1


class ChartsUiTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.hosts = []

    def tearDown(self):
        for host in self.hosts:
            host.page.setParent(None)
            host.page.deleteLater()
        self.app.processEvents()

    def host(self, records=(), with_refresh=True, raw_records=False):
        value = Host(records, with_refresh, raw_records)
        self.hosts.append(value)
        return value

    def test_charts_page_builds_all_cards_without_data(self):
        host = self.host([])
        container = host.charts_page()
        self.assertIsInstance(container, QWidget)
        self.assertIs(container.parent(), host.page)
        self.assertEqual(host.page_layout.count(), 1)
        self.assertGreaterEqual(len(container.findChildren(QFrame)), 5)  # 4 图表卡 + 预算卡
        self.assertEqual(len(container.findChildren(BarChart)), 3)
        self.assertEqual(len(container.findChildren(GroupedBarChart)), 1)
        for chart in container.findChildren(BarChart) + container.findChildren(GroupedBarChart):
            self.assertGreaterEqual(chart.minimumHeight(), 150)
        texts = ' '.join(label.text() for label in container.findChildren(QLabel))
        self.assertIn('暂无数据', texts)
        self.assertIn('刷新', ' '.join(b.text() for b in container.findChildren(QPushButton)))

    def test_charts_page_with_data_grab_and_stats(self):
        host = self.host(sample_records())
        container = host.charts_page()
        container.resize(1100, 760)
        charts = container.findChildren(BarChart)
        grouped = container.findChildren(GroupedBarChart)
        self.assertEqual(len(charts), 3)
        self.assertEqual(len(grouped), 1)
        module_chart = charts[0]
        modules = dict(module_chart.data())
        self.assertEqual(modules['任务管理'], 4)
        self.assertEqual(modules['风险台账'], 3)
        self.assertIn('共 9 条', module_chart.title())
        # 任务趋势：本周 2 条（1 已完成 / 1 未完成），其余周仍有柱位
        trend = grouped[0]
        self.assertEqual(len(trend.labels()), 8)
        self.assertEqual(trend.series()[0]['values'][-1], 1)   # 本周已完成
        self.assertEqual(trend.series()[1]['values'][-1], 1)   # 本周未完成
        self.assertEqual(sum(trend.series()[1]['values']), 2)  # t3 在上周、t4 无日期
        # 风险等级固定顺序：高/中/低/未填写（创建顺序：模块、趋势、风险、状态）
        risk_chart = charts[1]
        self.assertEqual([label for label, _ in risk_chart.data()],
                         ['高', '中', '低', '未填写'])
        self.assertEqual([count for _, count in risk_chart.data()], [1, 1, 0, 1])
        self.assertEqual(risk_chart.color(), '#b4453c')
        status_chart = charts[2]
        self.assertEqual(dict(status_chart.data())['进行中'], 2)
        self.assertEqual(dict(status_chart.data())['已完成'], 1)
        self.assertEqual(dict(status_chart.data()).get('未填写', 0), 0)
        texts = ' '.join(label.text() for label in container.findChildren(QLabel))
        self.assertIn('预算合计', texts)
        self.assertIn('1,000', texts)             # 金额来自 Decimal，千分位
        self.assertIn('250.50', texts)            # 非整数保留两位小数
        self.assertIn('100.0%', texts)            # 以预算合计为基数的占比
        for chart in charts + grouped:
            self.assertFalse(chart.grab().isNull(), chart.title())

    def test_charts_page_accepts_missing_attributes_and_refresh_fallback(self):
        host = self.host([])
        del host.records                                  # 缺 records 也不崩
        container = host.charts_page()
        self.assertIsInstance(container, QWidget)
        del host.refresh                                  # 无 refresh -> 就地重绘
        host.charts_page()
        button = next(b for b in container.findChildren(QPushButton) if b.text() == '刷新统计')
        button.click()                                    # 走 _charts_refresh 降级路径
        self.assertEqual(host.page_layout.count(), 1)     # 旧页面被替换而不是叠加

    def test_refresh_button_calls_host_refresh(self):
        host = self.host(sample_records())
        container = host.charts_page()
        button = next(b for b in container.findChildren(QPushButton) if b.text() == '刷新统计')
        button.click()
        self.assertEqual(host.refreshes, 1)
        self.assertTrue(button.property('primary'))

    def test_dashboard_charts_empty_and_full(self):
        empty = self.host([]).dashboard_charts()
        self.assertIsInstance(empty, QWidget)
        self.assertEqual(len(empty.findChildren(BarChart)), 2)
        self.assertEqual(len(empty.findChildren(GroupedBarChart)), 1)
        empty.resize(900, 220)
        self.assertFalse(empty.grab().isNull())

        host = self.host(sample_records())
        widget = host.dashboard_charts()
        self.assertEqual(len(widget.findChildren(BarChart)), 2)
        self.assertEqual(len(widget.findChildren(GroupedBarChart)), 1)
        self.assertEqual(dict(widget.findChildren(BarChart)[0].data())['预算费用'], 1)
        widget.resize(900, 220)
        pixmap = widget.grab()
        self.assertFalse(pixmap.isNull())
        self.assertGreater(pixmap.width(), 0)
        self.assertGreater(pixmap.height(), 0)
        self.assertEqual(pixmap.size(), widget.size())

    def test_dashboard_charts_survives_broken_records(self):
        host = self.host(None, raw_records=True)          # records=None
        widget = host.dashboard_charts()
        widget.resize(600, 200)
        self.assertFalse(widget.grab().isNull())
        host.records = [None, 'x', {}, {'module': 'tasks', 'data': None}, row('ok')]
        widget = host.dashboard_charts()
        widget.resize(600, 200)
        self.assertFalse(widget.grab().isNull())
        self.assertEqual(len(widget.findChildren(BarChart)), 2)

    def test_charts_paint_path_runs_for_small_and_tiny_geometries(self):
        host = self.host(sample_records())
        container = host.charts_page()
        for chart in container.findChildren(BarChart) + container.findChildren(GroupedBarChart):
            for width, height in ((0, 0), (40, 30), (200, 200)):
                chart.setMinimumSize(0, 0)
                chart.resize(width, height)
                pixmap = chart.grab()                     # 空/超小尺寸不得抛异常
                self.assertFalse(pixmap.isNull(), (chart.title(), width, height))
            chart.setMinimumHeight(200)
            chart.resize(360, 240)
            self.assertGreater(chart.grab().width(), 0)

    def test_bar_and_grouped_widgets_are_self_contained(self):
        bar = BarChart()
        bar.set_title('模块分布')
        bar.set_color('#b4453c')
        bar.set_data([('任务管理', 4), ('风险台账', 3)])
        bar.resize(320, 200)
        self.assertEqual(bar.title(), '模块分布')
        self.assertEqual(bar.color(), '#b4453c')
        self.assertEqual(bar.data(), [('任务管理', 4.0), ('风险台账', 3.0)])
        self.assertFalse(bar.grab().isNull())
        bar.set_data([])                                  # 空数据 -> 居中“暂无数据”
        self.assertFalse(bar.grab().isNull())

        grouped = GroupedBarChart()
        grouped.set_title('任务完成趋势')
        grouped.set_data(['03-03', '03-10'],
                         [{'name': '已完成', 'values': [1, 2], 'color': '#2563b8'},
                          {'name': '未完成', 'values': [3, 1], 'color': '#8fb2dd'}])
        grouped.resize(420, 240)
        self.assertEqual(grouped.labels(), ['03-03', '03-10'])
        self.assertEqual([item['name'] for item in grouped.series()], ['已完成', '未完成'])
        self.assertFalse(grouped.grab().isNull())
        grouped.set_data([], [])
        self.assertFalse(grouped.grab().isNull())


if __name__ == '__main__':
    import unittest
    unittest.main()
