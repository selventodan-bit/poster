"""Interactive node/port/edge editor. Coordinates are graph data, not an illustration."""

import tkinter as tk
from tkinter import ttk

from .model import GraphError


class GraphCanvas:
    WIDTH, HEIGHT = 180, 84

    def __init__(self, parent, document, *, selected, changed, editable):
        self.document = document
        self.on_selected, self.on_changed, self.editable = selected, changed, editable
        self.frame = ttk.Frame(parent)
        self.widget = tk.Canvas(self.frame, background="#f1f5f9", highlightthickness=0)
        vertical = ttk.Scrollbar(self.frame, orient="vertical", command=self.widget.yview)
        horizontal = ttk.Scrollbar(self.frame, orient="horizontal", command=self.widget.xview)
        self.widget.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.widget.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.frame.rowconfigure(0, weight=1)
        self.frame.columnconfigure(0, weight=1)
        self.selected = set()
        self.selected_edge = None
        self.connection = None
        self.drag = None
        self.results = {}
        self.node_boxes = {}
        self.port_points = {}
        self.widget.bind("<Button-1>", self.press)
        self.widget.bind("<B1-Motion>", self.motion)
        self.widget.bind("<ButtonRelease-1>", self.release)
        self.widget.bind("<Motion>", self.hover)
        self.widget.bind("<Delete>", lambda _: self.delete())
        self.widget.bind("<Control-c>", lambda _: self.copy())
        self.widget.bind("<Control-v>", lambda _: self.paste())
        self.widget.bind("<Control-z>", lambda _: self.undo())
        self.widget.bind(
            "<MouseWheel>", lambda e: self.widget.yview_scroll(-1 if e.delta > 0 else 1, "units")
        )
        self.widget.bind("<Button-4>", lambda _: self.widget.yview_scroll(-1, "units"))
        self.widget.bind("<Button-5>", lambda _: self.widget.yview_scroll(1, "units"))
        self.copied = set()
        self.draw()

    @property
    def graph(self):
        return self.document.graph

    @property
    def zoom(self):
        return self.graph.view.zoom

    def target(self):
        current = self.widget.find_withtag("current")
        for tag in self.widget.gettags(current[0]) if current else ():
            if tag.startswith(("port:", "node:", "edge:")):
                return tag.split(":")
        return []

    def draw(self):
        c, z = self.widget, self.zoom
        c.delete("all")
        self.node_boxes, self.port_points = {}, {}
        for node in self.graph.nodes:
            x, y = node.x * z, node.y * z
            cases = node.params.get("cases", []) if node.type == "Logic.Switch" else []
            height = self.HEIGHT + len(cases) * 22
            self.node_boxes[node.id] = (x, y, x + self.WIDTH * z, y + height * z)
            self.port_points[node.id, "input"] = (x, y + 42 * z)
            self.port_points[node.id, "success"] = (x + self.WIDTH * z, y + 28 * z)
            self.port_points[node.id, "error"] = (x + self.WIDTH * z, y + 65 * z)
            for index in range(len(cases)):
                self.port_points[node.id, "case:" + str(index)] = (
                    x + self.WIDTH * z,
                    y + (92 + index * 22) * z,
                )
        for index, edge in enumerate(self.graph.edges):
            start = self.port_points.get((edge.source, edge.port))
            end = self.port_points.get((edge.target, "input"))
            if not start or not end:
                continue
            color = "#15803d" if edge.port == "success" else "#dc2626" if edge.port == "error" else "#4f46e5"
            middle = max(start[0] + 25, (start[0] + end[0]) / 2)
            c.create_line(
                *start,
                middle,
                start[1],
                middle,
                end[1],
                *end,
                arrow="last",
                width=4 if self.selected_edge == index else 2,
                fill=color,
                tags=(f"edge:{index}",),
                smooth=False,
            )
        for node in self.graph.nodes:
            box = self.node_boxes[node.id]
            status = self.results.get(node.id, "")
            fill = {
                "RUNNING": "#fef08a",
                "SUCCEEDED": "#dcfce7",
                "FAILED": "#fee2e2",
                "CANCELLED": "#e2e8f0",
                "BREAKPOINT": "#fed7aa",
            }.get(status, "white")
            tag = f"node:{node.id}"
            c.create_rectangle(
                *box,
                fill=fill,
                outline="#2563eb" if node.id in self.selected else "#64748b",
                width=3 if node.id in self.selected else 1,
                tags=(tag,),
            )
            if node.breakpoint:
                c.create_oval(
                    box[0] - 5,
                    box[1] - 5,
                    box[0] + 8,
                    box[1] + 8,
                    fill="#dc2626",
                    outline="white",
                    tags=(tag,),
                )
            c.create_text(
                box[0] + 12 * z,
                box[1] + 13 * z,
                anchor="nw",
                text=node.title or node.type,
                width=148 * z,
                font=("TkDefaultFont", max(7, int(10 * z)), "bold"),
                tags=(tag,),
            )
            c.create_text(
                box[0] + 12 * z,
                box[1] + 55 * z,
                anchor="nw",
                text=status or ("Точка входа" if self.graph.entry == node.id else node.type),
                width=145 * z,
                font=("TkDefaultFont", max(6, int(8 * z))),
                tags=(tag,),
            )
            if node.type != "Start":
                self.port(node.id, "input", "#64748b")
            if node.type not in {"End", "End.Success", "End.Failure"}:
                self.port(node.id, "success", "#16a34a", "S")
                if node.type != "Start":
                    self.port(node.id, "error", "#dc2626", "E")
                for index, _case in enumerate(
                    node.params.get("cases", []) if node.type == "Logic.Switch" else []
                ):
                    self.port(node.id, "case:" + str(index), "#4f46e5", "C" + str(index + 1))
        bounds = c.bbox("all") or (0, 0, 800, 600)
        c.configure(
            scrollregion=(
                min(0, bounds[0] - 40),
                min(0, bounds[1] - 40),
                max(800, bounds[2] + 80),
                max(600, bounds[3] + 80),
            )
        )

    def port(self, node, name, color, label=""):
        x, y = self.port_points[node, name]
        radius = max(5, 7 * self.zoom)
        self.widget.create_oval(
            x - radius,
            y - radius,
            x + radius,
            y + radius,
            fill=color,
            outline="white",
            tags=(f"port:{node}:{name}",),
        )
        if label:
            self.widget.create_text(
                x - 15 * self.zoom,
                y,
                text=label,
                fill=color,
                font=("TkDefaultFont", max(7, int(8 * self.zoom))),
                tags=(f"port:{node}:{name}",),
            )

    def press(self, event):
        self.widget.focus_set()
        target = self.target()
        if not target:
            self.selected = set()
            self.selected_edge = None
            self.connection = None
            self.draw()
            self.on_selected(None)
            return
        if target[0] == "edge":
            self.selected = set()
            self.selected_edge = int(target[1])
            self.draw()
            self.on_selected(None)
            return
        node_id = target[1]
        if self.connection and (target[0] == "node" or target[-1] == "input"):
            if self.editable():
                try:
                    self.document.connect(*self.connection, node_id)
                    self.on_changed()
                except GraphError:
                    pass
            self.connection = None
            self.draw()
            return
        if target[0] == "port" and target[2] != "input":
            if self.editable():
                self.connection = (node_id, ":".join(target[2:]))
            return
        if event.state & 0x0001:
            if node_id in self.selected:
                self.selected.remove(node_id)
            else:
                self.selected.add(node_id)
        elif node_id not in self.selected:
            self.selected = {node_id}
        self.selected_edge = None
        self.on_selected(node_id)
        if self.editable():
            self.drag = (
                self.widget.canvasx(event.x),
                self.widget.canvasy(event.y),
                {i: (self.graph.node(i).x, self.graph.node(i).y) for i in self.selected},
                False,
            )
        self.draw()

    def motion(self, event):
        if not self.drag:
            return
        x, y, positions, checkpoint = self.drag
        dx = (self.widget.canvasx(event.x) - x) / self.zoom
        dy = (self.widget.canvasy(event.y) - y) / self.zoom
        if abs(dx) + abs(dy) < 2:
            return
        if not checkpoint:
            self.document.checkpoint()
            checkpoint = True
        for node_id, (old_x, old_y) in positions.items():
            node = self.graph.node(node_id)
            node.x = max(-100000, min(100000, old_x + dx))
            node.y = max(-100000, min(100000, old_y + dy))
        self.drag = (x, y, positions, checkpoint)
        self.draw()

    def release(self, _):
        if self.drag and self.drag[3]:
            self.on_changed()
        self.drag = None

    def hover(self, event):
        if self.connection:
            self.widget.delete("preview-link")
            point = self.port_points.get(self.connection)
            if point:
                self.widget.create_line(
                    *point,
                    self.widget.canvasx(event.x),
                    self.widget.canvasy(event.y),
                    dash=(4, 3),
                    arrow="last",
                    tags=("preview-link",),
                )
                self.widget.tag_lower("preview-link")

    def delete(self):
        if not self.editable():
            return
        edge = self.graph.edges[self.selected_edge] if self.selected_edge is not None else None
        self.document.delete(self.selected, edge)
        self.selected = set()
        self.selected_edge = None
        self.draw()
        self.on_selected(None)
        self.on_changed()

    def copy(self):
        self.copied = set(self.selected)

    def paste(self):
        if not self.editable():
            return
        ids = self.copied & {n.id for n in self.graph.nodes}
        if ids:
            self.selected = set(self.document.clone(ids))
            self.draw()
            self.on_selected(next(iter(self.selected)))
            self.on_changed()

    def duplicate(self):
        self.copy()
        self.paste()

    def undo(self):
        if self.editable():
            self.document.undo()
            self.selected = set()
            self.selected_edge = None
            self.draw()
            self.on_selected(None)
            self.on_changed()

    def fit(self):
        if not self.graph.nodes:
            return
        max_x = max(n.x + self.WIDTH for n in self.graph.nodes) + 50
        max_y = max(n.y + self.HEIGHT for n in self.graph.nodes) + 40
        self.graph.view.zoom = max(
            0.25,
            min(
                1.5,
                (self.widget.winfo_width() - 20) / max(max_x, 1),
                (self.widget.winfo_height() - 20) / max(max_y, 1),
            ),
        )
        self.draw()
        self.widget.xview_moveto(0)
        self.widget.yview_moveto(0)

    def positions(self):
        result = {}
        for key, box in self.node_boxes.items():
            x = (box[0] + box[2]) / 2 - self.widget.canvasx(0)
            y = (box[1] + box[3]) / 2 - self.widget.canvasy(0)
            if 0 <= x <= self.widget.winfo_width() and 0 <= y <= self.widget.winfo_height():
                result[key] = [self.widget.winfo_rootx() + int(x), self.widget.winfo_rooty() + int(y)]
        return result

    def dispose(self):
        self.widget = self.frame = None
