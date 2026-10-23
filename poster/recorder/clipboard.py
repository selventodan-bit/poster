"""Native Tk clipboard behavior retained from the main branch fixes."""

import sys
import tkinter as tk
from tkinter import ttk


class ClipboardMixin:
    def _enable_clipboard(self, widget):
        """Keyboard and right-click editing, including with a Russian keyboard layout."""
        widget.bind("<Control-KeyPress>", self._shortcut, add="+")
        menu = tk.Menu(widget, tearoff=0)
        for caption, action in (
            ("Вырезать", "cut"),
            ("Копировать", "copy"),
            ("Вставить", "paste"),
            ("Выделить всё", "all"),
        ):
            menu.add_command(
                label=caption,
                command=lambda a=action, w=widget: self._edit(w, a),
            )
        widget.bind("<Button-3>", lambda event, m=menu: self._popup(event, m), add="+")

    @staticmethod
    def _popup(event, menu):
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _shortcut(self, event):
        key = event.keysym.lower()
        actions = {
            "c": "copy",
            "v": "paste",
            "x": "cut",
            "a": "all",
            "cyrillic_es": "copy",
            "cyrillic_em": "paste",
            "cyrillic_che": "cut",
            "cyrillic_ef": "all",
        }
        action = actions.get(key)
        if sys.platform == "win32":
            # Windows virtual-key codes identify the physical C/V/X/A keys.
            action = {67: "copy", 86: "paste", 88: "cut", 65: "all"}.get(event.keycode, action)
        if action:
            return self._edit(event.widget, action)
        return None

    @staticmethod
    def _selection(widget):
        if isinstance(widget, (ttk.Entry, tk.Entry)):
            if widget.selection_present():
                return widget.get()[widget.index("sel.first") : widget.index("sel.last")]
        elif isinstance(widget, tk.Text) and widget.tag_ranges("sel"):
            return widget.get("sel.first", "sel.last")
        return None

    @staticmethod
    def _editable(widget):
        return str(widget.cget("state")) == "normal"

    def _edit(self, widget, action):
        entry = isinstance(widget, (ttk.Entry, tk.Entry))
        text_widget = isinstance(widget, tk.Text)
        if action == "all":
            if entry:
                widget.selection_range(0, "end")
                widget.icursor("end")
            elif text_widget:
                widget.tag_add("sel", "1.0", "end-1c")
            return "break"
        if action in {"copy", "cut"}:
            selected = self._selection(widget)
            if selected is None:
                return "break"
            self.root.clipboard_clear()
            self.root.clipboard_append(selected)
            self.root.update_idletasks()
            if action == "copy" or not self._editable(widget):
                return "break"
            if entry or text_widget:
                widget.delete("sel.first", "sel.last")
            return "break"
        if action == "paste":
            if not self._editable(widget):
                return "break"
            try:
                value = self.root.clipboard_get()
            except tk.TclError:
                return "break"
            if entry:
                selected = widget.selection_present()
                index = widget.index("sel.first") if selected else widget.index("insert")
                if selected:
                    widget.delete("sel.first", "sel.last")
                widget.insert(index, value)
            elif text_widget:
                selected = bool(widget.tag_ranges("sel"))
                index = widget.index("sel.first" if selected else "insert")
                if selected:
                    widget.delete("sel.first", "sel.last")
                widget.insert(index, value)
            return "break"
        return None
