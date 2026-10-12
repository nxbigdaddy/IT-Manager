"""Global cross-module search dialog and direct record jump."""
from __future__ import annotations

from PySide6.QtCore import Qt, QStringListModel
from PySide6.QtWidgets import (
    QComboBox, QCompleter, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .global_search import group_counts, search, suggestions
from .schema import MODULES

COLUMNS = ['模块', '事项', '负责人', '状态', '日期', '匹配分', '命中摘要']


class GlobalSearchDialog(QDialog):
    def __init__(self, host, query=''):
        super().__init__(host)
        self.host = host
        self.records = list(getattr(host, 'records', []) or [])
        self.results = []
        self.setWindowTitle('全局检索 · 跨模块快速跳转')
        self.resize(1100, 720)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('搜索任务、系统、资产、人员、风险、合同、专项相关记录。'
                                '全部关键词必须同时命中；结果按标题和字段匹配度排序。'
                                '搜索只使用已刷新业务记录，不读取附件正文、密钥或凭据值。'))
        tools = QHBoxLayout()
        layout.addLayout(tools)
        self.query = QLineEdit(query)
        self.query.setPlaceholderText('输入多个关键词，例如：备份 张工 高风险')
        model = QStringListModel(suggestions(self.records, '', 20), self.query)
        completer = QCompleter(model, self.query)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        self.query.setCompleter(completer)
        tools.addWidget(self.query, 3)
        self.module = QComboBox()
        self.module.addItem('全部模块', '')
        for key, (title, _) in MODULES.items():
            self.module.addItem(title, key)
        tools.addWidget(self.module, 1)
        run = QPushButton('搜索')
        run.setProperty('primary', True)
        run.clicked.connect(self.run_search)
        tools.addWidget(run)
        self.summary = QLabel('')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.doubleClicked.connect(self.open_selected)
        layout.addWidget(self.table, 1)
        footer = QHBoxLayout()
        layout.addLayout(footer)
        self.message = QLabel('双击结果可跳转模块并打开记录。')
        footer.addWidget(self.message)
        footer.addStretch()
        close = QPushButton('关闭')
        close.clicked.connect(self.reject)
        footer.addWidget(close)
        self.query.returnPressed.connect(self.run_search)
        self.module.currentIndexChanged.connect(lambda _: self.run_search() if self.query.text().strip() else None)
        if query.strip():
            self.run_search()
        else:
            self.query.setFocus()

    def run_search(self):
        text = self.query.text().strip()
        if not text:
            self.results = []
            self.table.setRowCount(0)
            self.summary.setText('请输入搜索词；空搜索不会展示全库内容，避免无意暴露敏感资料。')
            return
        modules = [self.module.currentData()] if self.module.currentData() else None
        try:
            value = search(self.records, text, modules=modules, limit=200)
        except ValueError as exc:
            self.summary.setText(str(exc))
            return
        self.results = value['results']
        self.table.setRowCount(len(self.results))
        for row, result in enumerate(self.results):
            values = [result.get('module_title', ''), result.get('title', ''), result.get('owner', ''),
                      result.get('status', ''), result.get('date', ''), result.get('score', ''),
                      result.get('snippet', '')]
            for column, text_value in enumerate(values):
                item = QTableWidgetItem(str(text_value))
                item.setData(Qt.UserRole, result)
                self.table.setItem(row, column, item)
        groups = group_counts(self.results)
        grouped = '；'.join(f'{name} {count}' for name, count in groups.items()) or '无'
        self.summary.setText(f"找到 {value['total']} 条（展示 {len(self.results)} 条）｜{grouped}")
        self.message.setText('双击结果可跳转模块并打开记录。' if self.results else
                             '没有匹配项；可减少关键词或切换为全部模块。')

    def selected(self):
        item = self.table.item(self.table.currentRow(), 0)
        return item.data(Qt.UserRole) if item else None

    def open_selected(self, *_):
        result = self.selected()
        if not result:
            return
        module, record = result.get('module'), result.get('record')
        if module not in getattr(self.host, 'keys', []) or not isinstance(record, dict):
            self.message.setText('记录已经失效，请关闭本窗口并刷新后重试。')
            return
        self.host.nav.setCurrentRow(self.host.keys.index(module))
        self.accept()
        # For archived rows or a stale search snapshot, opening the form would fail.
        if record.get('archived'):
            self.host.statusBar().showMessage('该搜索结果已归档，仅跳转到模块列表。')
            return
        self.host.edit_record(module, record)


def open_global_search(host, query=''):
    dialog = GlobalSearchDialog(host, query)
    dialog.exec()
    return dialog
