"""Marimo-native event callbacks; reactive evaluation never submits a run."""

import asyncio
import json

from .errors import CloudError
from .launcher import Launcher


class MarimoLauncher:
    def __init__(self, provider="molab", **kwargs):
        import marimo as mo

        self.backend = Launcher(provider, **kwargs)
        self.get_revision, self.set_revision = mo.state(0)
        self.message = "Подключите Drive через Remote Storage в боковой панели Molab, затем нажмите «Подключить». Автономный режим DETACHED."
        self.project_id = self.version = None
        self.count, self.parallel = 1, 1
        self.input_json, self.binding_json = "{}", "{}"
        self.tasks = set()
        self.inspection = dict(inputs=[], bindings={})

    def changed(self, message):
        self.message = message
        self.set_revision(lambda n: n + 1)

    def spawn(self, coroutine):
        async def safe():
            try:
                await coroutine
            except Exception as exc:
                self.changed(
                    exc.message
                    if isinstance(exc, CloudError)
                    else "Операция остановлена; выполнение не повторялось."
                )

        task = asyncio.create_task(safe())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return None

    async def connect(self):
        rows = await self.backend.connect()
        if rows:
            await self.select(rows[0]["project_id"])
        else:
            self.changed("На Drive пока нет опубликованных проектов Poster.")

    async def select(self, project_id):
        self.project_id = project_id
        rows = await self.backend.versions(project_id)
        self.version = rows[-1]["version"] if rows else None
        if self.version:
            self.inspection = await self.backend.inspect(project_id, self.version)
        self.changed(
            "Проект выбран. Максимум одновременно: " + str(self.backend.session.capacity.max_parallel)
        )

    async def execute(self):
        self.changed("EXECUTING — выполняется автономный запуск.")
        result = await self.backend.run(
            self.project_id,
            self.version,
            count=int(self.count),
            parallel=int(self.parallel),
            inputs=json.loads(self.input_json),
            bindings=json.loads(self.binding_json),
        )
        self.changed("Drive: " + ", ".join(r["status"] for r in result["runs"]))

    async def choose_version(self, version):
        self.version = version
        self.inspection = await self.backend.inspect(self.project_id, version)
        self.changed("Версия выбрана; проверьте входы и имена защищённых bindings.")

    def prepare(self):
        try:
            self.backend.prepare()
            self.changed("Новая заявка подготовлена. Нажмите «Выполнить», чтобы разрешить запуск.")
        except CloudError as exc:
            self.changed(exc.message)
        return None

    def render(self):
        import marimo as mo

        self.get_revision()
        rows = [p for p in self.backend.projects if p["status"] == "READY"]
        project_options = {p["name"] + " — " + p["project_id"][:8]: p["project_id"] for p in rows}
        version_options = {r["version"][:12]: r["version"] for r in reversed(self.backend.history)}
        project_label = next((k for k, v in project_options.items() if v == self.project_id), None)
        version_label = next((k for k, v in version_options.items() if v == self.version), None)
        maximum = self.backend.session.capacity.max_parallel if self.backend.session else 1
        return mo.vstack(
            [
                mo.md(
                    "## Poster — проекты Google Drive\nDETACHED: результаты сохраняются отдельно от управляемой очереди Windows."
                ),
                mo.md(self.message),
                mo.ui.button(label="Подключить Drive", on_click=lambda _: self.spawn(self.connect())),
                mo.ui.dropdown(
                    project_options,
                    value=project_label,
                    label="Проект",
                    on_change=lambda v: self.spawn(self.select(v)) if v else None,
                ),
                mo.ui.dropdown(
                    version_options,
                    value=version_label,
                    label="Версия",
                    on_change=lambda v: self.spawn(self.choose_version(v)) if v else None,
                ),
                mo.ui.number(
                    1,
                    1000,
                    value=self.count,
                    label="Всего запусков",
                    on_change=lambda v: setattr(self, "count", v),
                ),
                mo.ui.number(
                    1,
                    maximum,
                    value=min(self.parallel, maximum),
                    label="Одновременно",
                    on_change=lambda v: setattr(self, "parallel", v),
                ),
                mo.ui.text_area(
                    value=self.input_json,
                    label="Обычные входы JSON",
                    on_change=lambda v: setattr(self, "input_json", v),
                ),
                mo.plain_text(
                    "Входы: "
                    + ", ".join(d["name"] for d in self.inspection["inputs"])
                    + "; защищённые bindings: "
                    + ", ".join(self.inspection["bindings"])
                ),
                mo.md(
                    "Защищённые значения — в private environment Molab. Здесь только соответствия ref/вход → POSTER_SECRET_имя, без значений."
                ),
                mo.ui.text_area(
                    value=self.binding_json,
                    label="Имена защищённых bindings JSON",
                    on_change=lambda v: setattr(self, "binding_json", v),
                ),
                mo.hstack(
                    [
                        mo.ui.button(label="Подготовить новый запуск", on_click=lambda _: self.prepare()),
                        mo.ui.button(label="Выполнить", on_click=lambda _: self.spawn(self.execute())),
                        mo.ui.button(label="Отменить", on_click=lambda _: self.spawn(self.backend.cancel())),
                        mo.ui.button(
                            label="Завершить сессию", on_click=lambda _: self.spawn(self.backend.close())
                        ),
                    ]
                ),
            ]
        )
