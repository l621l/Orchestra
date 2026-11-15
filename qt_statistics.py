"""Project usage summary and per-model details."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout,
)


def count(value):
    return f'{value:,}'.replace(',', '\u202f')


def cache_hit_rate(entry):
    prompt_tokens = entry['prompt_tokens']
    return f"{entry['cached_tokens'] / prompt_tokens:.1%}" if prompt_tokens else '—'


class UsageDialog(QDialog):
    def __init__(self, parent, data):
        super().__init__(parent)
        self.setWindowTitle('Статистика проекта')
        self.resize(790, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        total = data['total']
        summary = QHBoxLayout()
        for title, value in (
            ('Расход проекта', f"${total['estimated_usd']:.5f}"),
            ('Всего токенов', count(total['total_tokens'])),
            ('Попадание в кэш', cache_hit_rate(total)),
            ('Запуски', count(data['runs'])),
            ('Модели', count(len(data['models']))),
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

        entries = [('Весь проект', total)] + [
            (f"{model['provider']} / {model['model']}", model) for model in data['models']]
        table = QTableWidget(len(entries), 6)
        table.setHorizontalHeaderLabels(['Модель', 'Расход, USD', 'Токены', 'Кэш', 'Запуски', 'Вызовы API'])
        table.horizontalHeaderItem(3).setToolTip('Доля входных токенов, прочитанных из кэша')
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
        details.addRow('Токены', token_details)
        details.addRow('Кэш', cache_details)
        details.addRow('Данные API', reported)
        layout.addLayout(details)

        def show_details(row, *_):
            if row < 0:
                return
            _, entry = entries[row]
            token_details.setText(
                f"Вход {count(entry['prompt_tokens'])} · Выход {count(entry['completion_tokens'])} · "
                f"Рассуждения {count(entry['reasoning_tokens'])}")
            cache_details.setText(
                f"Чтение {count(entry['cached_tokens'])} · Запись {count(entry['cache_write_tokens'])}")
            suffix = '' if entry['complete'] else ' · сумма неполная'
            reported.setText(f"{count(entry['reported_calls'])} из {count(entry['api_calls'])} вызовов{suffix}")

        table.currentCellChanged.connect(show_details)
        table.setCurrentCell(0, 0)
