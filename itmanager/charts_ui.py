"""统计看板页面（ChartsMixin），供 ``MainWindow`` 多继承。

宿主只需提供 ``records`` / ``page_layout``（可选 ``refresh``）；所有统计都基于
``self.records`` 在本地计算，不发网络请求、不用后台线程、不修改 ``generation``。

``charts.py`` 的纯函数负责取数，本模块只负责排版。
"""
from __future__ import annotations

import json
from datetime import date

from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

from .charts import (
    THEME, BarChart, GroupedBarChart, budget_totals, module_totals, percent,
    risk_levels, status_breakdown, top_n, undated_open_tasks, weekly_task_trend,
)
from .schema import MODULES

CHART_TITLE = '统计看板'
TREND_WEEKS = 8
SERIES_DONE = '#2563b8'
SERIES_OPEN = '#8fb2dd'
RISK_COLORS = {'高': '#b4453c', '中': '#c98a2b', '低': '#3f7d5a', '未填写': '#8fb2dd'}


def _money(value):
    """Decimal -> 千分位文本，保留两位小数并去掉多余的 .00。"""
    try:
        text = f'{value:,.2f}'
    except (TypeError, ValueError, AttributeError):
        return str(value)
    return text[:-3] if text.endswith('.00') else text


def _card(title, hint=''):
    """主题一致的卡片：白底、圆角、细边框，返回 (frame, 内容布局)。"""
    frame = QFrame()
    frame.setProperty('card', True)
    frame.setStyleSheet(
        f'QFrame[card="true"]{{background:{THEME["card"]};border:1px solid '
        f'{THEME["border"]};border-radius:9px;}}')
    frame.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 10, 12, 10)
    layout.setSpacing(6)
    heading = QHBoxLayout()
    label = QLabel(title)
    label.setStyleSheet(f'font-size:15px;font-weight:600;color:{THEME["deep"]}')
    heading.addWidget(label)
    heading.addStretch()
    if hint:
        note = QLabel(hint)
        note.setStyleSheet(f'color:{THEME["muted"]}')
        heading.addWidget(note)
    layout.addLayout(heading)
    return frame, layout


class ChartsMixin:
    """统计看板：``charts_page()`` 作为导航页，``dashboard_charts()`` 供工作台嵌入。"""

    # ------------------------------------------------------------------ 页面
    def charts_page(self):
        """在 ``self.page_layout`` 构建“统计看板”页；空数据时给出可操作的空态。"""
        layout = getattr(self, 'page_layout', None)
        if layout is None:                                   # 安全降级
            return None
        records = self._chart_records()
        container = QWidget()
        page = QVBoxLayout(container)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(10)

        info = QLabel(f'统计范围：全部未归档记录 · 数据截至 {date.today().isoformat()} · '
                      f'共 {len(records)} 条 · 本地计算，不发起网络请求')
        info.setWordWrap(True)
        info.setStyleSheet(f'color:{THEME["muted"]}')
        page.addWidget(info)

        toolbar = QHBoxLayout()
        summary = QLabel('')
        toolbar.addWidget(summary)
        toolbar.addStretch()
        refresh = QPushButton('刷新统计')
        refresh.setProperty('primary', True)
        refresh.clicked.connect(self._charts_refresh)
        toolbar.addWidget(refresh)
        export = QPushButton('导出图表与数据…')
        export.clicked.connect(lambda: self._charts_export(grid))
        toolbar.addWidget(export)
        page.addLayout(toolbar)

        grid = QGridLayout()
        grid.setSpacing(10)
        page.addLayout(grid)

        modules = module_totals(records)
        module_chart = BarChart()
        module_chart.set_title(f'模块记录数 · 共 {sum(count for _, count in modules)} 条')
        module_chart.set_data(modules)
        module_chart.clicked.connect(self._charts_drill_module)
        grid.addWidget(self._chart_card(module_chart, '按模块统计未归档记录数；点击柱条进入该模块'), 0, 0)

        trend = weekly_task_trend(records, TREND_WEEKS)
        trend_chart = GroupedBarChart()
        undated = undated_open_tasks(records)
        trend_chart.set_title(f'任务完成趋势 · 最近 {TREND_WEEKS} 周')
        trend_chart.set_data([item['label'] for item in trend],
                             [{'name': '已完成', 'values': [item['done'] for item in trend],
                               'color': SERIES_DONE},
                              {'name': '未完成', 'values': [item['open'] for item in trend],
                               'color': SERIES_OPEN}])
        grid.addWidget(self._chart_card(
            trend_chart, f'按截止日期分周（周一为首）· 无日期任务 {undated} 条未计入'), 0, 1)

        risks = risk_levels(records)
        risk_chart = BarChart()
        risk_chart.set_title(f'风险等级 · 共 {sum(count for _, count in risks)} 项')
        risk_chart.set_data(risks)
        risk_chart.set_color(RISK_COLORS['高'])
        risk_chart.clicked.connect(lambda _label: self._charts_drill_module('风险台账'))
        grid.addWidget(self._chart_card(risk_chart, '风险台账按等级分组；点击柱条进入风险台账'), 1, 0)

        statuses = status_breakdown(records, 'tasks')
        status_chart = BarChart()
        status_chart.set_title(f'任务状态分布 · 共 {sum(count for _, count in statuses)} 条')
        status_chart.set_data(top_n(statuses, 10))
        grid.addWidget(self._chart_card(status_chart, '任务管理状态字段分组，其余计入其他'), 1, 1)

        totals = budget_totals(records)
        budget_card, budget_layout = _card('预算汇总', '单位：元 · 非法金额已跳过')
        if totals:
            table = QGridLayout()
            table.setHorizontalSpacing(18)
            base = totals.get('预算合计')
            for row, (key, value) in enumerate(totals.items()):
                name = QLabel(key)
                name.setStyleSheet(f'color:{THEME["muted"]}')
                amount = QLabel(_money(value))
                amount.setStyleSheet(f'font-weight:600;color:{THEME["deep"]}')
                share = QLabel('' if base is None else f'{percent(value, base):.1f}%')
                share.setStyleSheet(f'color:{THEME["muted"]}')
                table.addWidget(name, row, 0)
                table.addWidget(amount, row, 1)
                table.addWidget(share, row, 2)
            table.setColumnStretch(3, 1)
            budget_layout.addLayout(table)
        else:
            hint = QLabel('暂无预算数据：在「预算费用」中登记金额后，这里会自动汇总。')
            hint.setWordWrap(True)
            hint.setStyleSheet(f'color:{THEME["muted"]}')
            budget_layout.addWidget(hint)
        budget_layout.addStretch()
        grid.addWidget(budget_card, 2, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        if not records:
            empty = QLabel('暂无数据：先点击顶部「刷新」同步工作库，或在「任务管理 / 预算费用」中新建记录；'
                           '有了记录后本页图表会自动统计。')
            empty.setWordWrap(True)
            empty.setStyleSheet('background:#eaf2fd;border-radius:8px;padding:14px;'
                                f'color:{THEME["deep"]}')
            page.addWidget(empty)

        summary.setText(f'模块 {len(modules)} 个 · 任务状态 {len(statuses)} 类 · '
                        f'风险 {sum(count for _, count in risks)} 项')
        page.addStretch()
        layout.addWidget(container, 1)
        return container

    # -------------------------------------------------------------- 工作台嵌入
    def _charts_drill_module(self, label):
        """Drill down from a chart bar into the matching business module list."""
        keys = getattr(self, 'keys', None)
        navigate = getattr(self, 'nav', None)
        if not keys or navigate is None:
            return
        for module, (title, _fields) in MODULES.items():
            if title == label and module in keys:
                self.statusBar().showMessage(f'已按图表筛选：{title}') if hasattr(self, 'statusBar') else None
                navigate.setCurrentRow(keys.index(module))
                return
        if hasattr(self, 'statusBar'):
            self.statusBar().showMessage(f'“{label}”没有对应的业务模块列表。')

    def _charts_export(self, grid):
        """Export the visible charts as PNG images plus a CSV/JSON data snapshot."""
        from PySide6.QtWidgets import QFileDialog
        folder = QFileDialog.getExistingDirectory(self, '选择导出文件夹（图表 PNG 与数据 JSON）')
        if not folder:
            return
        from pathlib import Path
        target = Path(folder)
        saved, failures = [], []
        charts = grid.parentWidget().findChildren(BarChart) + grid.parentWidget().findChildren(GroupedBarChart)
        for index, chart in enumerate([item for item in charts if getattr(item, 'data', None)
                                       or getattr(item, 'labels', None)], start=1):
            name = (chart.title() or f'chart-{index}').replace('/', '_').replace('\\', '_')
            path = target / f'{index:02d}-{name}.png'
            try:
                if not chart.grab().save(str(path)):
                    failures.append(path.name)
                else:
                    saved.append(path.name)
            except Exception:
                failures.append(path.name)
        payload = {}
        for chart in charts:
            key = chart.title() or f'chart-{len(payload) + 1}'
            if isinstance(chart, GroupedBarChart):
                payload[key] = {'labels': chart.labels(),
                                'series': [{'name': row['name'], 'values': list(row['values'])}
                                           for row in chart.series()]}
            else:
                payload[key] = [{'label': label, 'value': value} for label, value in (chart.data() or [])]
        try:
            (target / 'charts-data.json').write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as exc:
            failures.append('charts-data.json（' + str(exc) + '）')
        message = f'已导出 {len(saved)} 张图表与数据文件到 {folder}。'
        if failures:
            message += ' 失败：' + '、'.join(failures)
        if hasattr(self, 'statusBar'):
            self.statusBar().showMessage(message)

    def dashboard_charts(self):
        """返回紧凑工作台图表控件（模块分布 / 任务趋势 / 风险等级）。

        仅依赖 ``self.records``；``records`` 为空或缺省时同样可安全构建。
        """
        records = self._chart_records()
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        modules = module_totals(records)
        module_chart = BarChart(container)
        module_chart.set_title('模块分布')
        module_chart.set_data(modules)
        module_chart.setMinimumHeight(170)
        layout.addWidget(self._chart_card(module_chart, f'{len(records)} 条未归档记录'), 1)

        trend = weekly_task_trend(records, TREND_WEEKS)
        trend_chart = GroupedBarChart(container)
        trend_chart.set_title(f'任务趋势 · {TREND_WEEKS} 周')
        trend_chart.set_data([item['label'] for item in trend],
                             [{'name': '已完成', 'values': [item['done'] for item in trend],
                               'color': SERIES_DONE},
                              {'name': '未完成', 'values': [item['open'] for item in trend],
                               'color': SERIES_OPEN}])
        trend_chart.setMinimumHeight(190)
        layout.addWidget(self._chart_card(
            trend_chart, f'无日期 {undated_open_tasks(records)} 条未计入'), 1)

        risks = risk_levels(records)
        risk_chart = BarChart(container)
        risk_chart.set_title('风险等级')
        risk_chart.set_data(risks)
        risk_chart.set_color(RISK_COLORS['高'])
        risk_chart.setMinimumHeight(170)
        layout.addWidget(self._chart_card(risk_chart, f'{sum(c for _, c in risks)} 项'), 1)
        return container

    # -------------------------------------------------------------------- 内部
    def _chart_records(self):
        records = getattr(self, 'records', None)
        return [r for r in records if isinstance(r, dict)] if isinstance(records, (list, tuple)) else []

    def _charts_refresh(self):
        """优先复用宿主刷新（含网络同步），否则就地重绘当前页面。"""
        callback = getattr(self, 'refresh', None)
        if callable(callback):
            callback()
            return
        layout = getattr(self, 'page_layout', None)
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.charts_page()

    @staticmethod
    def _chart_card(chart, hint=''):
        frame, layout = _card(chart.title() or '统计', hint)
        chart.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(chart, 1)
        return frame
