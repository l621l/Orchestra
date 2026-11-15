"""Selectable commit graph for the Qt desktop."""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFontDatabase, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsRectItem, QGraphicsScene, QGraphicsView


COLORS = ('#71b7ff', '#ffb86c', '#78dca8', '#d6a0ff', '#ff858d', '#f2d477')
ROW_HEIGHT = 34
LANE_WIDTH = 22


def layout_commits(history):
    lanes = []
    rows = []
    width = 1
    for line in history.splitlines():
        if '\0' not in line:
            continue
        commit, parents, short, refs, subject = line.split('\0', 4)
        if commit not in lanes:
            lanes.append(commit)
        previous = lanes.copy()
        lane = lanes.index(commit)
        lanes.pop(lane)
        parents = parents.split()
        for offset, parent in enumerate(parents):
            if parent not in lanes:
                lanes.insert(min(lane + offset, len(lanes)), parent)
        edges = [(index, lanes.index(ref)) for index, ref in enumerate(previous) if ref != commit]
        edges.extend((lane, lanes.index(parent)) for parent in parents)
        rows.append((commit, lane, edges, short, refs.strip().removeprefix('(').removesuffix(')'), subject))
        width = max(width, len(previous), len(lanes))
    return rows, width


class CommitRow(QGraphicsRectItem):
    def __init__(self, commit, rect, *, is_head=False):
        super().__init__(rect)
        self.commit = commit
        self.is_head = is_head
        self.hovered = False
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setPen(QPen(Qt.PenStyle.NoPen))

    def paint(self, painter, option, widget=None):
        if self.isSelected():
            painter.fillRect(self.rect(), QColor('#2b4c6b'))
        elif self.hovered:
            painter.fillRect(self.rect(), QColor('#303b48'))
        elif self.is_head:
            painter.fillRect(self.rect(), QColor('#21374d'))
        if self.is_head:
            rect = self.rect()
            painter.fillRect(QRectF(rect.left(), rect.top(), 3, rect.height()), QColor('#75b9ff'))

    def hoverEnterEvent(self, event):
        self.hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.scene().clearSelection()
            self.setSelected(True)
            event.accept()
        else:
            super().mousePressEvent(event)


class GitGraph(QGraphicsView):
    def __init__(self, history, parent=None, *, head=None):
        super().__init__(parent)
        scene = QGraphicsScene(self)
        self.setScene(scene)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setBackgroundBrush(QColor('#16181a'))
        rows, lanes = layout_commits(history)
        if not rows:
            self.label('В репозитории пока нет коммитов.', 16, 24, '#e8eaf0')
            return

        labels_x = 26 + lanes * LANE_WIDTH
        backgrounds = []
        for index, (commit, lane, edges, short, refs, subject) in enumerate(rows):
            y = (index + 0.5) * ROW_HEIGHT
            background = CommitRow(commit, QRectF(0, index * ROW_HEIGHT, 1, ROW_HEIGHT),
                                   is_head=commit == head)
            background.setZValue(-2)
            scene.addItem(background)
            backgrounds.append(background)
            if index < len(rows) - 1:
                for start, end in edges:
                    x1, x2 = 18 + start * LANE_WIDTH, 18 + end * LANE_WIDTH
                    path = QPainterPath()
                    path.moveTo(x1, y)
                    path.cubicTo(x1, y + 14, x2, y + 20, x2, y + ROW_HEIGHT)
                    edge = scene.addPath(path, QPen(QColor(COLORS[end % len(COLORS)]), 2))
                    edge.setZValue(-1)
                    edge.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            x = 18 + lane * LANE_WIDTH
            dot = scene.addEllipse(x - 5, y - 5, 10, 10, QPen(QColor('#16181a'), 1),
                                   QBrush(QColor(COLORS[lane % len(COLORS)])))
            dot.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.label(short, labels_x, y, '#9bb4cf', fixed=True)
            subject_x = labels_x + 88
            if refs:
                label = self.label(refs, subject_x + 6, y, '#afe2ff')
                badge = scene.addRect(label.sceneBoundingRect().adjusted(-5, -2, 5, 2),
                                      QPen(Qt.PenStyle.NoPen), QBrush(QColor('#283f55')))
                badge.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
                label.setZValue(1)
                subject_x = badge.rect().right() + 10
            self.label(subject, subject_x, y, '#e8eaf0')

        width = max(780, scene.itemsBoundingRect().right() + 20)
        scene.setSceneRect(0, 0, width, len(rows) * ROW_HEIGHT)
        for row in backgrounds:
            rect = row.rect()
            rect.setWidth(width)
            row.setRect(rect)

    def label(self, text, x, y, color, *, fixed=False):
        label = self.scene().addSimpleText(text)
        if fixed:
            label.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        label.setBrush(QColor(color))
        label.setPos(x, y - label.boundingRect().height() / 2)
        label.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        return label
