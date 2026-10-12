"""Chart drill-down, PNG/data export and the AI weekly-summary entry points."""
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from unittest.mock import patch

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QFileDialog

from itmanager.charts import BarChart
from itmanager.gui import MainWindow


class Client:
    url = 'http://127.0.0.1:18765'

    def __init__(self):
        self.calls = []

    def get(self, path):
        self.calls.append(('GET', path))
        return []

    def post(self, path, payload):
        self.calls.append(('POST', path))
        return {'id': 'DRAFT-1', 'title': 'AI周报草稿', 'content': payload.get('content', '')}


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


def build(qapp):
    window = MainWindow(Client(), autoload=False)
    window.records = [
        {'id': 'T1', 'module': 'tasks', 'version': 1, 'archived': 0,
         'data': {'名称': '演示任务', '状态': '进行中', '截止日期': '2030-01-01'}},
        {'id': 'R1', 'module': 'risks', 'version': 1, 'archived': 0,
         'data': {'名称': '演示风险', '等级': '高', '状态': '进行中'}},
    ]
    window.navigate(window.keys.index('charts'))
    return window


def test_bar_chart_emits_click_for_the_bar_under_the_pointer(qapp):
    chart = BarChart()
    chart.resize(300, 200)
    chart.set_data([('甲', 3), ('乙', 5), ('丙', 1)])
    chart.show()
    qapp.processEvents()
    seen = []
    chart.clicked.connect(seen.append)
    hit = chart.item_at(QPoint(int(chart._plot.left() + chart._slot * 1.5), int(chart._plot.center().y())))
    assert hit and hit[1] == '乙'
    assert chart.item_at(QPoint(0, 0)) is None
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QPointF, QEvent
    position = QPointF(chart._plot.left() + chart._slot * 1.5, chart._plot.center().y())
    event = QMouseEvent(QEvent.MouseButtonRelease, position, chart.mapToGlobal(position),
                        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    chart.mouseReleaseEvent(event)
    assert seen == ['乙']
    chart.deleteLater()


def test_module_chart_click_navigates_to_that_module(qapp):
    window = build(qapp)
    assert window.current == 'charts'
    window._charts_drill_module('任务管理')
    assert window.current == 'tasks'
    window.navigate(window.keys.index('charts'))
    window._charts_drill_module('不存在的模块')
    assert window.current == 'charts'


def test_charts_export_writes_png_and_json(qapp, tmp_path):
    window = build(qapp)
    from PySide6.QtWidgets import QGridLayout
    grid = window.page.findChild(QGridLayout)
    assert grid is not None
    with patch.object(QFileDialog, 'getExistingDirectory', return_value=str(tmp_path)):
        window._charts_export(grid)
    pngs = sorted(path.name for path in tmp_path.glob('*.png'))
    assert pngs, 'at least one chart image must be exported'
    data = json.loads((tmp_path / 'charts-data.json').read_text(encoding='utf-8'))
    assert any(entry for entry in data.values())
    assert '已导出' in window.statusBar().currentMessage()


def test_charts_export_cancel_writes_nothing(qapp, tmp_path):
    window = build(qapp)
    from PySide6.QtWidgets import QGridLayout
    grid = window.page.findChild(QGridLayout)
    with patch.object(QFileDialog, 'getExistingDirectory', return_value=''):
        window._charts_export(grid)
    assert not list(tmp_path.glob('*.png'))
