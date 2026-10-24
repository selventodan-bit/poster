"""Native indexed graph scene. Rendering/editing only; never executes actions."""

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsEllipseItem, QGraphicsPathItem, QGraphicsPolygonItem
from PySide6.QtWidgets import QGraphicsRectItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView


def outputs(node):
    if node.type in {"End", "End.Success", "End.Failure"}:
        return []
    cases = (
        ["case:" + str(i) for i in range(len(node.params.get("cases", [])))]
        if node.type == "Logic.Switch"
        else []
    )
    return ["success", "error", *cases]


class Port(QGraphicsEllipseItem):
    def __init__(self, owner, name, point):
        super().__init__(-5, -5, 10, 10, owner)
        self.owner, self.name = owner, name
        self.setPos(point)
        self.setBrush(QColor("#dc2626" if name == "error" else "#16a34a" if name == "success" else "#2563eb"))
        self.setToolTip(name)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def mousePressEvent(self, event):
        self.owner.canvas.port_clicked(self.owner.node.id, self.name)
        event.accept()


class Block(QGraphicsRectItem):
    def __init__(self, canvas, node):
        ports = outputs(node)
        super().__init__(0, 0, 210, max(90, 43 + len(ports) * 17))
        self.canvas, self.node = canvas, node
        self.setFlags(
            self.GraphicsItemFlag.ItemIsMovable
            | self.GraphicsItemFlag.ItemIsSelectable
            | self.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setPos(node.x, node.y)
        self.setToolTip(node.type + "\n" + node.id)
        self.title = QGraphicsSimpleTextItem(
            ("● " if node.breakpoint else "") + (node.title or node.type)[:27], self
        )
        self.title.setPos(9, 6)
        self.type_label = QGraphicsSimpleTextItem(node.type[:30], self)
        self.type_label.setPos(9, 25)
        self.ports = {}
        if node.type != "Start":
            self.ports["input"] = Port(self, "input", QPointF(0, 45))
        for index, name in enumerate(ports):
            y = 46 + index * 17
            self.ports[name] = Port(self, name, QPointF(210, y))
            label = QGraphicsSimpleTextItem(name, self)
            label.setPos(197 - label.boundingRect().width(), y - 8)
        self.status = None
        self.refresh()

    def refresh(self):
        color = {
            "RUNNING": "#fef08a",
            "SUCCEEDED": "#dcfce7",
            "FAILED": "#fee2e2",
            "CANCELLED": "#e5e7eb",
        }.get(self.status, "#f8fafc")
        self.setBrush(QColor(color))
        self.setPen(QPen(QColor("#2563eb" if self.isSelected() else "#64748b"), 2))

    def itemChange(self, change, value):
        if change == self.GraphicsItemChange.ItemPositionHasChanged and hasattr(self, "canvas"):
            self.canvas.update_edges(self.node.id)
        if change == self.GraphicsItemChange.ItemSelectedHasChanged:
            self.refresh()
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        if self.canvas.connection and self.node.type != "Start":
            self.canvas.port_clicked(self.node.id, "input")
            return
        self.setFlag(self.GraphicsItemFlag.ItemIsMovable, self.canvas.editable())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.canvas.commit_positions()


class GraphView(QGraphicsView):
    selected = Signal(object)
    changed = Signal()
    problem = Signal(object)

    def __init__(self, document, editable):
        super().__init__()
        self.setObjectName("graph_canvas")
        self.document, self.editable = document, editable
        self.blocks, self.links, self.adjacent = {}, [], {}
        self.connection = None
        self.render_serial = 0
        self.scene_model = QGraphicsScene(self)
        self.setScene(self.scene_model)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(self.DragMode.RubberBandDrag)
        self.setTransformationAnchor(self.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(self.ViewportUpdateMode.MinimalViewportUpdate)
        self.scene_model.selectionChanged.connect(self.selection_changed)
        self.redraw()

    def paintEvent(self, event):
        super().paintEvent(event)
        self.render_serial += 1

    def redraw(self):
        selected = self.selection()
        self.scene_model.blockSignals(True)
        self.blocks, self.links, self.adjacent = {}, [], {}
        self.scene_model.clear()
        for node in self.document.graph.nodes:
            block = Block(self, node)
            self.blocks[node.id] = block
            self.scene_model.addItem(block)
            block.setSelected(node.id in selected)
        for edge in self.document.graph.edges:
            path = QGraphicsPathItem()
            path.setPen(QPen(QColor("#dc2626" if edge.port == "error" else "#64748b"), 2))
            path.setFlags(path.GraphicsItemFlag.ItemIsSelectable)
            path.setZValue(-1)
            arrow = QGraphicsPolygonItem(path)
            arrow.setBrush(QColor("#64748b"))
            self.scene_model.addItem(path)
            link = (edge, path, arrow)
            self.links.append(link)
            self.adjacent.setdefault(edge.source, []).append(link)
            self.adjacent.setdefault(edge.target, []).append(link)
            self.draw_edge(link)
        self.scene_model.setSceneRect(self.scene_model.itemsBoundingRect().adjusted(-500, -500, 500, 500))
        self.scene_model.blockSignals(False)
        self.selection_changed()

    def draw_edge(self, link):
        edge, item, arrow = link
        source, target = self.blocks.get(edge.source), self.blocks.get(edge.target)
        if not source or not target or edge.port not in source.ports or "input" not in target.ports:
            return
        a, b = source.ports[edge.port].scenePos(), target.ports["input"].scenePos()
        bend = max(60, abs(b.x() - a.x()) / 2)
        path = QPainterPath(a)
        path.cubicTo(a + QPointF(bend, 0), b - QPointF(bend, 0), b)
        item.setPath(path)
        arrow.setPolygon(QPolygonF([b, b + QPointF(-11, -5), b + QPointF(-11, 5)]))

    def update_edges(self, node_id):
        for link in self.adjacent.get(node_id, []):
            self.draw_edge(link)

    def commit_positions(self):
        moved = [b for b in self.blocks.values() if (b.node.x, b.node.y) != (b.x(), b.y())]
        if moved and self.editable():
            self.document.checkpoint()
            for block in moved:
                block.node.x, block.node.y = block.x(), block.y()
            self.changed.emit()
            self.scene_model.setSceneRect(self.scene_model.itemsBoundingRect().adjusted(-500, -500, 500, 500))

    def selection(self):
        return [item.node.id for item in self.scene_model.selectedItems() if isinstance(item, Block)]

    def selection_changed(self):
        self.selected.emit(self.selection())

    def port_clicked(self, node_id, name):
        if not self.editable():
            return
        if name == "input" and self.connection:
            source, port = self.connection
            self.connection = None
            try:
                self.document.connect(source, port, node_id)
                self.redraw()
                self.changed.emit()
            except Exception as error:
                self.problem.emit(error)
        elif name != "input":
            self.connection = (node_id, name)
            self.setToolTip("Выберите вход следующего кубика. Escape — отмена.")

    def delete(self):
        if not self.editable():
            return
        ids = set(self.selection())
        edges = [edge for edge, path, _ in self.links if path.isSelected()]
        if ids or edges:
            self.document.delete(ids)
            self.document.graph.edges = [e for e in self.document.graph.edges if e not in edges]
            self.redraw()
            self.changed.emit()

    def copy(self):
        if self.editable() and self.selection():
            ids = self.document.clone(set(self.selection()))
            self.redraw()
            self.choose(ids)
            self.changed.emit()

    def choose(self, ids):
        self.scene_model.clearSelection()
        for node_id in ids:
            if node_id in self.blocks:
                self.blocks[node_id].setSelected(True)

    def follow(self, node_id, status="RUNNING"):
        block = self.blocks.get(node_id)
        if block:
            block.status = status
            block.refresh()
            self.ensureVisible(block, 35, 35)

    def find(self, query):
        for node in self.document.graph.nodes:
            if query.casefold() in (node.title + " " + node.type + " " + node.id).casefold():
                self.choose([node.id])
                self.centerOn(self.blocks[node.id])
                return node.id

    def fit(self):
        if self.blocks:
            self.fitInView(
                self.scene_model.itemsBoundingRect().adjusted(-25, -25, 25, 25),
                Qt.AspectRatioMode.KeepAspectRatio,
            )
            zoom = max(0.25, min(3, self.transform().m11()))
            self.resetTransform()
            self.scale(zoom, zoom)
            self.document.graph.view.zoom = zoom
            self.viewport().update()

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            zoom = max(
                0.25, min(3, self.transform().m11() * (1.15 if event.angleDelta().y() > 0 else 1 / 1.15))
            )
            self.resetTransform()
            self.scale(zoom, zoom)
            self.document.graph.view.zoom = zoom
            event.accept()
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self.pan_origin = event.position()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.MiddleButton and hasattr(self, "pan_origin"):
            delta = event.position() - self.pan_origin
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(delta.y()))
            self.pan_origin = event.position()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete:
            self.delete()
        elif event.key() == Qt.Key.Key_Escape:
            self.connection = None
        else:
            super().keyPressEvent(event)
