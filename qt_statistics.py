"""Project usage summary and per-model details."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)


def count(value):
    return f'{value:,}'.replace(',', '\u202f')


def cache_hit_rate(entry):
    prompt_tokens = entry['prompt_tokens']
    return f"{entry['cached_tokens'] / prompt_tokens:.1%}" if prompt_tokens else '—'


class UsageDialog(QDialog):
    def __init__(self, parent, data):
        super().__init__(parent)
        self.setWindowTitle('Project Usage')
        self.resize(1100, 620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        total = data['total']
        summary = QHBoxLayout()
        for title, value in (
            ('Project cost', f"${total['estimated_usd']:.5f}"),
            ('Total tokens', count(total['total_tokens'])),
            ('Cache hit rate', cache_hit_rate(total)),
            ('Runs', count(data['runs'])),
            ('Models', count(len(data['models']))),
        ):
            card = QFrame()
            card.setObjectName('usageCard')
            card.setStyleSheet(
                'QFrame#usageCard { border: 1px solid #3d4b5b; border-radius: 8px; }')
            contents = QVBoxLayout(card)
            contents.addWidget(QLabel(title))
            metric = QLabel(value)
            metric.setStyleSheet('font-size: 21px; font-weight: 600; color: #a7d6ff;')
            contents.addWidget(metric)
            summary.addWidget(card, 1)
        layout.addLayout(summary)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        overview = QWidget()
        layout = QVBoxLayout(overview)
        tabs.addTab(overview, 'Models')
        tabs.addTab(activity_table(data.get('stages', [])), 'By stage')
        tabs.addTab(activity_table(reversed(data.get('history', [])), history=True), 'Task history')

        entries = [('All models', total)] + [
            (f"{model['provider']} / {model['model']}", model) for model in data['models']]
        table = QTableWidget(len(entries), 6)
        table.setHorizontalHeaderLabels(['Model', 'Cost (USD)', 'Tokens', 'Cache', 'Runs', 'API calls'])
        table.horizontalHeaderItem(3).setToolTip('Share of input tokens served from cache')
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().hide()
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for row, (title, entry) in enumerate(entries):
            cells = (title, f"{entry['estimated_usd']:.5f}", count(entry['total_tokens']), cache_hit_rate(entry),
                     count(entry['runs']), count(entry['api_calls']))
            for column, value in enumerate(cells):
                item = QTableWidgetItem(value)
                if column:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row, column, item)
        layout.addWidget(table, 1)

        details = QFormLayout()
        token_details = QLabel()
        cache_details = QLabel()
        reported = QLabel()
        details.addRow('Tokens', token_details)
        details.addRow('Cache', cache_details)
        details.addRow('API reporting', reported)
        layout.addLayout(details)

        def show_details(row, *_):
            if row < 0:
                return
            _, entry = entries[row]
            token_details.setText(
                f"Input {count(entry['prompt_tokens'])} · Output {count(entry['completion_tokens'])} · "
                f"Reasoning {count(entry['reasoning_tokens'])}")
            cache_details.setText(
                f"Reads {count(entry['cached_tokens'])} · Writes {count(entry['cache_write_tokens'])}")
            suffix = '' if entry['complete'] else ' · partial total'
            reported.setText(f"{count(entry['reported_calls'])} of {count(entry['api_calls'])} calls{suffix}")

        table.currentCellChanged.connect(show_details)
        table.setCurrentCell(0, 0)


def activity_table(entries, *, history=False):
    entries = list(entries)
    columns = ['Model', 'Stage', 'Cost (USD)', 'Time', 'Calls', 'Tokens', 'Edits',
               'Lines added', 'Lines removed']
    if history:
        columns = ['Task', 'Run (UTC)', 'Status', *columns, 'Files changed']
    table = QTableWidget(len(entries), len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setAlternatingRowColors(True)
    table.verticalHeader().hide()
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for row, entry in enumerate(entries):
        cost = entry['estimated_usd']
        stage = {'implement': 'Implementation', 'review': 'Review'}.get(entry['stage'], entry['stage'])
        cells = [f"{entry['provider']} / {entry['model']}", stage,
                 f'{cost:.5f}' if cost is not None else 'Unknown',
                 f"{entry['elapsed_seconds']:.1f} s", count(entry['api_calls']),
                 count(entry['total_tokens']), count(entry['edits']),
                 count(entry['lines_added']), count(entry['lines_removed'])]
        if history:
            cells = [entry['task'], entry['run_id'].removeprefix('turn_').rsplit('_', 1)[0],
                     f"{entry['stage_status']} / {entry['run_status']}", *cells,
                     count(len(entry['files']))]
        for column, value in enumerate(cells):
            item = QTableWidgetItem(value)
            item.setToolTip('\n'.join(entry['files']) if history and column == len(cells) - 1 else value)
            table.setItem(row, column, item)
    table.setToolTip('Lines count successful tool edits, including rework. Time includes model and tool calls. '
                     'Stage breakdown is available for runs recorded by this version.')
    return table
