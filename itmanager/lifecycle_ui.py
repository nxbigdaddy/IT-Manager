"""Read-only lifecycle view: 事件 → 根因 → 变更 → 验证 → 复盘, with explicit gaps."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from .lifecycle import STAGE_LABELS, STAGES, chain, summarize_chain


class LifecycleDialog(QDialog):
    """Shows a record's chain computed from the loaded ledger; never claims it is resolved."""

    def __init__(self, records, record_id, parent=None):
        super().__init__(parent)
        try:
            self.result = chain(records, record_id)
        except ValueError as exc:
            self.result = None
            self.error = str(exc)
        else:
            self.error = ''
        self.setWindowTitle('关联链条（只读）')
        self.resize(880, 640)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('按记录之间的关联字段双向展开：事件 → 根因 → 变更 → 验证 → 复盘。'
                                '“完整”只表示三个阶段都有记录，不代表问题已经解决；'
                                '缺口提示仅说明缺少可核对的记录，不会替你判断结论。'))
        body = QPlainTextEdit()
        body.setReadOnly(True)
        body.setPlainText(self.describe())
        layout.addWidget(body, 1)
        tools = QHBoxLayout()
        layout.addLayout(tools)
        tools.addStretch()
        close = QPushButton('关闭')
        close.clicked.connect(self.accept)
        tools.addWidget(close)

    def describe(self):
        if self.error:
            return '无法构建链条：' + self.error
        result = self.result
        summary = summarize_chain(result)
        lines = [f"起点：{result['root'].get('title', '')}（{result['root'].get('module', '')}）",
                 f"节点总数：{summary['total']}｜记录齐备性：{'三阶段齐备' if summary['complete'] else '尚不齐备'}",
                 '']
        by_stage = {}
        for node in result['nodes']:
            by_stage.setdefault(node.get('stage'), []).append(node)
        for stage in STAGES:
            nodes = by_stage.get(stage, [])
            lines.append(f"【{STAGE_LABELS[stage]}】{len(nodes)} 条")
            for node in nodes:
                lines.append(f"  · {node.get('title', '')}｜{node.get('status') or '状态未填'}"
                             f"｜{node.get('date') or '日期未填'}｜深度 {node.get('depth')}")
            if not nodes:
                lines.append('  · 无')
        lines.append('')
        lines.append('缺口提示：')
        lines.extend(['  · ' + gap for gap in result.get('gaps', [])] or ['  · 暂未发现明显缺口'])
        related = result.get('related') or {}
        for bucket, rows in related.items():
            lines.append(f'相关 {bucket}：' + ('、'.join(row.get('title', '') for row in rows) or '无'))
        return '\n'.join(lines)


def open_lifecycle_dialog(host, record):
    """Entry point for the record toolbar: needs a saved record id."""
    if not record or not record.get('id'):
        host.statusBar().showMessage('请先选择已保存的记录。')
        return None
    dialog = LifecycleDialog(getattr(host, 'records', []) or [], record['id'], host)
    dialog.exec()
    return dialog
