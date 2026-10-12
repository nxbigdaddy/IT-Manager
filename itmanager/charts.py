"""Dashboard statistics: pure aggregation helpers plus self-drawn chart widgets.

Two deliberately separate layers:

* pure functions (no Qt, deterministic, directly unit-testable):
  :func:`module_totals`, :func:`status_breakdown`, :func:`weekly_task_trend`,
  :func:`undated_open_tasks`, :func:`risk_levels`, :func:`budget_totals`,
  :func:`percent`, :func:`top_n`.
* painting-only widgets: :class:`BarChart`, :class:`GroupedBarChart`.

Records follow the store shape ``{'id':..., 'module':..., 'data': {...}, 'archived': 0}``.
Archived records (``archived`` truthy) are ignored by every statistic here, matching
the default of ``store.list_records`` / ``GET /records``.

The widgets intentionally avoid ``PySide6.QtCharts``: a plain ``QWidget`` with a
``QPainter`` keeps the light theme identical to the rest of the workspace and stays
testable (``grab()`` exercises the whole paint path).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .schema import MODULES

# Theme colours reused from workspace_ui.QSS so the charts match the light theme.
THEME = {
    'deep': '#173b65',
    'mid': '#24588a',
    'accent': '#2563b8',
    'soft': '#dceaff',
    'canvas': '#f5f7fb',
    'card': '#ffffff',
    'border': '#dde4ee',
    'text': '#243249',
    'muted': '#7d8aa0',
}
PALETTE = ('#2563b8', '#24588a', '#173b65', '#5b8fd6', '#8fb2dd', '#b9cde8')

BAR_COLOR = THEME['accent']
MAX_BARS = 12
EMPTY_TEXT = '暂无数据'

#: Real field names of the ``budget`` module in ``itmanager.schema``.
BUDGET_FIELDS = ('预算', '已发生', '已承诺未发生', '预计新增')
RISK_LEVELS = ('高', '中', '低', '未填写')
DONE_STATES = ('已完成',)

# Tolerated money decorations: thousands separators, full/half width yen signs.
_MONEY_NOISE = re.compile(r'[,\s，、]|人民币|RMB|CNY|￥|¥|元')


def _archived(record):
    return bool((record or {}).get('archived'))


def _data(record):
    value = (record or {}).get('data')
    return value if isinstance(value, dict) else {}


def _active_records(records):
    """Iterate non-archived, dict-shaped records safely."""
    if not isinstance(records, (list, tuple)):
        return []
    return [r for r in records if isinstance(r, dict) and not _archived(r)]


def _text(value):
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _iso_date(value):
    """Parse a leading YYYY-MM-DD (also accepts a date/datetime). Invalid -> None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = _text(value)[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _decimal(raw):
    """Parse a non-negative decimal. Anything invalid or negative -> None (skipped).

    Accepts surrounding whitespace, thousands separators, currency words/symbols and
    full-width parentheses for negatives (``（1,200）`` -> -1200 -> rejected).
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float, Decimal)):
        text = str(raw)
    else:
        text = _text(raw)
    if not text:
        return None
    negative = (text.startswith('(') and text.endswith(')')) or \
               (text.startswith('（') and text.endswith('）'))
    text = text.strip('()（）')
    text = _MONEY_NOISE.sub('', text)
    if not text:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if negative:
        value = -value
    if not value.is_finite() or value < 0:
        return None
    return value


# ---------------------------------------------------------------- pure functions

def module_totals(records):
    """Count non-archived records per ``MODULES`` title.

    Returns ``[(中文模块名, 数量), ...]`` sorted by count descending, then by the
    Chinese name ascending (stable ordering for equal counts). Records whose
    ``module`` key is unknown or missing are ignored entirely.
    """
    counts = {}
    for record in _active_records(records):
        module = record.get('module')
        if module not in MODULES:
            continue
        counts[module] = counts.get(module, 0) + 1
    rows = [(MODULES[module][0], count) for module, count in counts.items()]
    rows.sort(key=lambda item: (-item[1], item[0]))
    return rows


def status_breakdown(records, module):
    """Group one module's records by the ``状态`` field.

    Records with a missing/blank status are counted as ``未填写``. Returns
    ``[(状态, 数量), ...]`` sorted by count descending, then status text ascending.
    An unknown ``module`` yields ``[]``.
    """
    counts = {}
    for record in _active_records(records):
        if record.get('module') != module:
            continue
        status = _text(_data(record).get('状态')) or '未填写'
        counts[status] = counts.get(status, 0) + 1
    rows = list(counts.items())
    rows.sort(key=lambda item: (-item[1], item[0]))
    return rows


def weekly_task_trend(records, weeks=8, today=None):
    """Weekly buckets of task counts, keyed by the deadline field ``截止日期``.

    A week starts on Monday; ``label`` is ``'MM-DD'`` of that Monday. The result holds
    the most recent ``weeks`` buckets **including the current week**, in ascending time
    order (so the last item is the current week), each ``{'label', 'done', 'open'}``:

    * ``done`` – tasks whose ``状态`` is ``已完成``;
    * ``open`` – every other task with a deadline in that week (never closed by 状态).

    Tasks without a usable deadline are **ignored here** (they belong to no week); use
    :func:`undated_open_tasks` for that number. Out-of-range deadlines (before the first
    bucket or after the current week) are ignored as well.

    ``weeks`` is clamped to ``>= 0``; ``0`` returns ``[]``. ``today`` accepts a
    ``date``/``datetime`` or an ISO ``YYYY-MM-DD`` string and defaults to
    ``date.today()``.
    """
    try:
        weeks = int(weeks)
    except (TypeError, ValueError):
        weeks = 8
    if weeks <= 0:
        return []
    reference = _iso_date(today) or date.today()
    current_monday = reference - timedelta(days=reference.weekday())
    first_monday = current_monday - timedelta(weeks=weeks - 1)
    buckets = {(first_monday + timedelta(weeks=offset)): {'done': 0, 'open': 0}
               for offset in range(weeks)}
    for record in _active_records(records):
        if record.get('module') != 'tasks':
            continue
        deadline = _iso_date(_data(record).get('截止日期'))
        if deadline is None:
            continue
        monday = deadline - timedelta(days=deadline.weekday())
        bucket = buckets.get(monday)
        if bucket is None:
            continue
        key = 'done' if _text(_data(record).get('状态')) in DONE_STATES else 'open'
        bucket[key] += 1
    return [{'label': monday.strftime('%m-%d'), 'done': buckets[monday]['done'],
             'open': buckets[monday]['open']}
            for monday in sorted(buckets)]


def undated_open_tasks(records):
    """Number of non-archived tasks without a parseable ``截止日期`` (any 状态)."""
    total = 0
    for record in _active_records(records):
        if record.get('module') != 'tasks':
            continue
        if _iso_date(_data(record).get('截止日期')) is None:
            total += 1
    return total


def risk_levels(records):
    """Risk-ledger counts per ``等级``, in the fixed order 高, 中, 低, 未填写.

    Levels outside that set (including a missing/blank value) are counted as ``未填写``
    so the four buckets always add up to the number of non-archived risk records.
    Empty buckets are returned with ``0`` to keep the order stable for the chart.
    """
    counts = {level: 0 for level in RISK_LEVELS}
    for record in _active_records(records):
        if record.get('module') != 'risks':
            continue
        level = _text(_data(record).get('等级'))
        counts[level if level in counts else '未填写'] += 1
    return [(level, counts[level]) for level in RISK_LEVELS]


def budget_totals(records):
    """Sum the four real ``budget`` module money fields over non-archived records.

    Keys are exactly ``'预算合计'``, ``'已发生合计'``, ``'承诺合计'``, ``'预计新增合计'``
    (the last three abbreviate ``已承诺未发生`` / ``预计新增``). A key is present only when
    at least one record supplied a parseable value for it, so an empty ledger yields
    ``{}``. Values are ``decimal.Decimal`` parsed as non-negative decimals; illegal,
    negative or blank values are skipped silently instead of raising.
    """
    totals = {}
    for field, key in zip(BUDGET_FIELDS, ('预算合计', '已发生合计', '承诺合计', '预计新增合计')):
        found = None
        for record in _active_records(records):
            if record.get('module') != 'budget':
                continue
            if field not in _data(record):
                continue
            value = _decimal(_data(record).get(field))
            if value is None:
                continue
            found = value if found is None else found + value
        if found is not None:
            totals[key] = found
    return totals


def percent(value, total):
    """``value / total * 100`` as ``float``; ``0.0`` when ``total <= 0`` or unusable."""
    try:
        total = float(total)
    except (TypeError, ValueError):
        return 0.0
    if total <= 0:
        return 0.0
    try:
        return float(value) / total * 100.0
    except (TypeError, ValueError):
        return 0.0


def top_n(items, n, other_label='其他'):
    """Keep the first ``n`` items of ``(label, value)`` and fold the rest into one row.

    Input order is preserved (sort beforehand if a ranking is wanted) and the tail is
    collapsed to a single ``(other_label, 合计)`` tuple appended last. The tail row is
    omitted when nothing was dropped. ``n <= 0`` keeps only the folded tail (or ``[]``
    for empty input); a non-positive tail total is still returned as ``0``.
    """
    rows = [(label, value) for label, value in items]
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = len(rows)
    if n < 0:
        n = 0
    if len(rows) <= n:
        return rows
    kept = rows[:n]
    rest = rows[n:]
    total = 0
    for _, value in rest:
        try:
            total += value
        except TypeError:
            continue
    kept.append((other_label, total))
    return kept


# ---------------------------------------------------------------------- painting

def _short(text, limit=8):
    text = _text(text)
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _draw_empty(painter, widget):
    """Centered ``暂无数据`` hint; never raises for tiny or zero-sized widgets."""
    if widget.width() <= 0 or widget.height() <= 0:
        return
    painter.setPen(QPen(QColor(THEME['muted'])))
    painter.drawText(widget.rect(), Qt.AlignCenter, EMPTY_TEXT)


class BarChart(QWidget):
    """Vertical bar chart with value labels, drawn with ``QPainter``.

    Public API: ``set_data(list[tuple[str, float]])``, ``set_color(color)``,
    ``set_title(text)``. More than :data:`MAX_BARS` items are truncated for display and
    the title gains a ``(前12项)`` suffix. Empty data paints a centered ``暂无数据``.
    Clicking a bar emits :attr:`clicked` with the category label for drill-down.
    """

    clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data = []
        self._color = BAR_COLOR
        self._title = ''
        self._plot = None
        self._slot = 0.0
        self._visible = 0
        self._hover = -1
        self.setMinimumHeight(200)
        self.setMouseTracking(True)

    def item_at(self, point):
        """Index + label of the bar under ``point`` (None when outside a bar slot)."""
        if not self._plot or self._slot <= 0 or not self._visible:
            return None
        if not self._plot.contains(float(point.x()), self._plot.top()):
            return None
        index = int((float(point.x()) - self._plot.left()) // self._slot)
        if 0 <= index < self._visible:
            return index, self._data[index][0]
        return None

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt naming
        hit = self.item_at(event.position())
        if hit and event.button() == Qt.LeftButton:
            self.clicked.emit(hit[1])
            return
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt naming
        hit = self.item_at(event.position())
        index = hit[0] if hit else -1
        if index != self._hover:
            self._hover = index
            if hit:
                self.setToolTip(f'{hit[1]}：点击查看对应记录')
            else:
                self.setToolTip('')
            self.update()
        super().mouseMoveEvent(event)

    # -- public API ----------------------------------------------------------
    def set_data(self, data):
        rows = []
        for item in data or []:
            try:
                label, value = item
                rows.append((_text(label), float(value)))
            except (TypeError, ValueError):
                continue
        self._data = rows
        self.update()

    def data(self):
        return list(self._data)

    def set_color(self, color):
        self._color = str(color or BAR_COLOR)
        self.update()

    def color(self):
        return self._color

    def set_title(self, text):
        self._title = _text(text)
        self.update()

    def title(self):
        return self._title

    # -- painting ------------------------------------------------------------
    def paintEvent(self, event):  # noqa: N802 - Qt naming
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            if not self._data or self.width() <= 0 or self.height() <= 0:
                _draw_empty(painter, self)
                return
            rows = self._data[:MAX_BARS]
            truncated = len(self._data) > MAX_BARS
            title = self._title + ('(前12项)' if truncated else '')
            metrics = QFontMetrics(self.font())
            top = 6
            if title:
                painter.setPen(QPen(QColor(THEME['deep'])))
                painter.drawText(8, top + metrics.ascent(), _short(title, 40))
                top += metrics.height() + 4
            bottom_pad = metrics.height() + 6 if self.height() >= 120 else 4
            left, right = 8, max(0, self.width() - 8)
            plot = QRectF(left, top, max(0.0, right - left),
                          max(0.0, self.height() - top - bottom_pad - 4))
            if plot.width() <= 6 or plot.height() <= 6:
                painter.setPen(QPen(QColor(THEME['muted'])))
                painter.drawText(self.rect(), Qt.AlignCenter, EMPTY_TEXT)
                return
            maximum = max((abs(value) for _, value in rows), default=0.0) or 1.0
            slot = plot.width() / len(rows)
            gap = slot * 0.22
            bar_width = max(2.0, slot - gap)
            self._plot = plot
            self._slot = slot
            self._visible = len(rows)
            show_labels = bar_width >= 16 and plot.height() >= 40
            label_height = metrics.height() if show_labels else 0
            bar_area = max(1.0, plot.height() - label_height)
            painter.setPen(QPen(QColor(THEME['border'])))
            painter.drawLine(plot.left(), plot.bottom(), plot.right(), plot.bottom())
            for index, (label, value) in enumerate(rows):
                height = max(1.0, bar_area * (abs(value) / maximum))
                bar = QRectF(plot.left() + index * slot + gap / 2,
                             plot.bottom() - height, bar_width, height)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(self._color))
                painter.drawRoundedRect(bar, 3, 3)
                if not show_labels:
                    continue
                painter.setPen(QPen(QColor(THEME['text'])))
                painter.drawText(QRectF(bar.left() - gap / 2, bar.top() - metrics.height(),
                                        bar.width() + gap, metrics.height()),
                                 Qt.AlignCenter, f'{value:g}')
                painter.setPen(QPen(QColor(THEME['muted'])))
                painter.drawText(QRectF(plot.left() + index * slot, plot.bottom() + 2,
                                        slot, label_height),
                                 Qt.AlignCenter, _short(label, max(3, int(slot / 12))))
        finally:
            painter.end()


class GroupedBarChart(QWidget):
    """Grouped bars plus legend.

    ``set_data(labels, series)`` where ``series`` is
    ``[{'name': '已完成', 'values': [...], 'color': '#2563b8'}, ...]``. Labels are
    omitted (rather than raising) when the widget is too small to place them.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._labels = []
        self._series = []
        self._title = ''
        self.setMinimumHeight(220)

    # -- public API ----------------------------------------------------------
    def set_data(self, labels, series):
        self._labels = [_text(label) for label in (labels or [])]
        rows = []
        for index, item in enumerate(series or []):
            if not isinstance(item, dict):
                continue
            values = []
            for value in item.get('values') or []:
                try:
                    values.append(float(value))
                except (TypeError, ValueError):
                    values.append(0.0)
            rows.append({'name': _text(item.get('name')) or f'系列{index + 1}',
                         'values': values,
                         'color': str(item.get('color') or PALETTE[index % len(PALETTE)])})
        self._series = rows
        self.update()

    def set_series(self, series):
        self.set_data(self._labels, series)

    def series(self):
        return [dict(row) for row in self._series]

    def labels(self):
        return list(self._labels)

    def set_title(self, text):
        self._title = _text(text)
        self.update()

    def title(self):
        return self._title

    # -- painting ------------------------------------------------------------
    def paintEvent(self, event):  # noqa: N802 - Qt naming
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            count = min(len(self._labels), MAX_BARS)
            usable = [row for row in self._series if row['values']]
            if not count or not usable or self.width() <= 0 or self.height() <= 0:
                _draw_empty(painter, self)
                return
            metrics = QFontMetrics(self.font())
            top = 6
            if self._title:
                painter.setPen(QPen(QColor(THEME['deep'])))
                painter.drawText(8, top + metrics.ascent(), _short(self._title, 40))
                top += metrics.height() + 4
            legend_height = metrics.height() + 4 if len(usable) > 1 else 0
            bottom_pad = metrics.height() + 6
            plot = QRectF(8, top, max(0.0, self.width() - 16),
                          max(0.0, self.height() - top - bottom_pad - legend_height - 4))
            if plot.width() <= 6 or plot.height() <= 6:
                painter.setPen(QPen(QColor(THEME['muted'])))
                painter.drawText(self.rect(), Qt.AlignCenter, EMPTY_TEXT)
                return
            values = [value for row in usable for value in row['values'][:count]]
            maximum = max((abs(value) for value in values), default=0.0) or 1.0
            slot = plot.width() / count
            group_width = max(4.0, slot * 0.72)
            bar_width = max(1.0, group_width / len(usable))
            show_labels = slot >= 18 and plot.height() >= 44
            label_height = metrics.height() if show_labels else 0
            bar_area = max(1.0, plot.height() - label_height)
            painter.setPen(QPen(QColor(THEME['border'])))
            painter.drawLine(plot.left(), plot.bottom(), plot.right(), plot.bottom())
            for index in range(count):
                group_left = plot.left() + index * slot + (slot - group_width) / 2
                for order, row in enumerate(usable):
                    raw = row['values'][index] if index < len(row['values']) else 0.0
                    height = max(1.0, bar_area * (abs(raw) / maximum))
                    bar = QRectF(group_left + order * bar_width,
                                 plot.bottom() - height, max(1.0, bar_width - 1), height)
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(row['color']))
                    painter.drawRect(bar)
                    if show_labels:
                        painter.setPen(QPen(QColor(THEME['text'])))
                        painter.drawText(QRectF(bar.left() - 2, bar.top() - metrics.height(),
                                                bar.width() + 4, metrics.height()),
                                         Qt.AlignCenter, f'{raw:g}')
                if show_labels:
                    painter.setPen(QPen(QColor(THEME['muted'])))
                    painter.drawText(QRectF(plot.left() + index * slot, plot.bottom() + 2,
                                            slot, label_height),
                                     Qt.AlignCenter, _short(self._labels[index], 6))
            if legend_height:
                legend_y = self.height() - metrics.height() - 2
                cursor = 8
                painter.setPen(Qt.NoPen)
                for row in usable:
                    painter.setBrush(QColor(row['color']))
                    painter.drawRect(QRectF(cursor, legend_y + 3, 9, 9))
                    cursor += 13
                    painter.setPen(QPen(QColor(THEME['muted'])))
                    painter.drawText(cursor, legend_y + metrics.ascent(), _short(row['name'], 8))
                    cursor += metrics.horizontalAdvance(_short(row['name'], 8)) + 14
                    painter.setPen(Qt.NoPen)
        finally:
            painter.end()
