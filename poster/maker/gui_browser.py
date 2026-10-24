"""Provider/profile controls; browser execution remains outside the Tk process."""

import importlib.util
import json
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
import tkinter as tk

from .model import GraphError
from .profiles import Profiles
from .runtime import BrowserSession


class BrowserToolsMixin:
    def build_browser_controls(self):
        info = {}
        try:
            info = json.loads(Path("BUILD_INFO.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self.qt_available = (
            info.get("browser_provider") == "qt" or importlib.util.find_spec("PySide6") is not None
        )
        self.default_provider = "qt" if info.get("browser_provider") == "qt" else "chromium"
        self.provider_name = tk.StringVar(value=self.default_provider)
        self.profile_name = tk.StringVar(value="Без сохранённой сессии")
        self.profile_store = Profiles(self.workspace.root / "browser-profiles")
        row = ttk.Frame(self.frame)
        row.pack(fill="x")
        ttk.Label(row, text="Браузер:").pack(side="left")
        self.provider_combo = ttk.Combobox(
            row,
            textvariable=self.provider_name,
            state="readonly",
            width=12,
            values=("chromium", "qt") if self.qt_available else ("chromium",),
        )
        self.provider_combo.pack(side="left", padx=3)
        self.provider_combo.bind("<<ComboboxSelected>>", self.browser_config_changed)
        ttk.Label(row, text="Профиль:").pack(side="left")
        self.profile_combo = ttk.Combobox(row, textvariable=self.profile_name, state="readonly", width=23)
        self.profile_combo.pack(side="left", padx=3)
        self.profile_combo.bind("<<ComboboxSelected>>", self.browser_config_changed)
        self.button(row, "profile_create", "Создать", self.create_browser_profile)
        self.button(row, "profile_save", "Сохранить сессию", self.save_browser_session)
        self.button(row, "profile_clear", "Очистить профиль", self.clear_browser_profile)
        self.refresh_browser_controls()

    def refresh_browser_controls(self):
        if not hasattr(self, "profile_store"):
            return
        self.profile_options = {"Без сохранённой сессии": None}
        for key, name in self.profile_store.list():
            self.profile_options[name + " · " + key[-6:]] = key
        self.profile_combo.configure(values=tuple(self.profile_options))
        key = self.document.graph.metadata.get("browser_profile")
        self.profile_name.set(
            next(
                (name for name, value in self.profile_options.items() if value == key),
                "Без сохранённой сессии",
            )
        )
        provider = self.document.graph.metadata.get("browser_provider", self.default_provider)
        self.provider_name.set(provider if provider != "qt" or self.qt_available else "chromium")

    def browser_config_changed(self, event=None):
        if self.busy or self.recording or self.browser_session():
            self.refresh_browser_controls()
            self.status.set("Сначала нажмите «Остановить», чтобы закрыть текущую сессию браузера.")
            return
        graph = self.document.graph
        graph.metadata["browser_provider"] = self.provider_name.get()
        key = self.profile_options.get(self.profile_name.get())
        if key:
            graph.metadata["browser_profile"] = key
        else:
            graph.metadata.pop("browser_profile", None)
        self.changed()

    def new_browser_session(self):
        provider = self.provider_name.get()
        if provider == "qt" and not self.qt_available:
            raise GraphError("BROWSER_HOST_START_FAILED")
        key = self.document.graph.metadata.get("browser_profile")
        profile = self.profile_store.open(key) if key else None
        return BrowserSession(False, provider=provider, profile=profile)

    def create_browser_profile(self):
        if self.busy or self.recording or self.browser_session():
            self.status.set("Сначала нажмите «Остановить».")
            return
        name = simpledialog.askstring("Новый профиль", "Название профиля", parent=self.root)
        if name:
            try:
                key = self.profile_store.create(name)
                self.document.graph.metadata["browser_profile"] = key
                self.refresh_browser_controls()
                self.changed()
            except Exception as error:
                self.show_error(error)

    def save_browser_session(self):
        session = self.browser_session()
        if not session or not session.profile:
            self.status.set("Создайте или выберите профиль перед открытием браузера.")
            return
        if self.recording or self.busy:
            return

        async def work():
            await session.profile.save(session)

        self.submit("profile_save", work)
        self.status.set("Снимок cookies и localStorage будет зашифрован локальным ключом.")

    def clear_browser_profile(self):
        key = self.document.graph.metadata.get("browser_profile")
        if not key or self.busy or self.recording or self.browser_session():
            self.status.set("Выберите профиль и закройте браузер кнопкой «Остановить».")
            return
        if messagebox.askyesno(
            "Очистка профиля", "Удалить только сохранённую сессию выбранного профиля?", parent=self.root
        ):
            try:
                self.profile_store.clear(key)
                self.status.set("Сессия выбранного профиля очищена.")
            except Exception as error:
                self.show_error(error)
