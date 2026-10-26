"""Common notebook user workflow with independent Jupyter/marimo frontends."""

import asyncio
import json
from pathlib import Path
import tempfile
import uuid

from .errors import CloudError
from .providers import connect, resolve_bindings
from .runtime import NotebookSession


def project_inputs(bundle):
    from poster.maker.model import Graph, graph_secret_refs

    aliases, definitions = {}, []
    for name in bundle.manifest["graphs"]:
        graph = Graph.model_validate_json(bundle.files[name])
        for key, value in graph.variables.items():
            if name == bundle.manifest["main"] and value.input:
                definitions.append(
                    dict(
                        name=key,
                        protected=value.secret,
                        required=value.required,
                        has_default=value.value is not None,
                    )
                )
            if isinstance(value.value, dict) and set(value.value) == {"secret_ref"}:
                label = key if name == bundle.manifest["main"] else name + " / " + key
                aliases[label] = value.value["secret_ref"]
        for ref in graph_secret_refs(graph):
            if ref not in aliases.values():
                aliases[ref] = ref
    return dict(inputs=definitions, bindings=aliases)


class Launcher:
    def __init__(self, provider="generic", *, connection=None, root=None):
        self.provider = provider
        self.root = Path(root) if root else Path(tempfile.mkdtemp(prefix="poster-notebook-"))
        self.connection = connection
        self.session = None
        self.prepared = uuid.uuid4().hex
        self.ticket = None
        self.last = None
        self.projects = []
        self.history = []

    async def connect(self):
        if self.connection is None:
            self.connection = await connect(self.provider, self.root)
        if self.session is None:
            self.session = NotebookSession(self.connection.library, self.root, provider=self.provider)
        self.projects = await self.session.catalog()
        return self.projects

    async def versions(self, project_id):
        self.history = await self.session.versions(project_id)
        return self.history

    async def inspect(self, project_id, version):
        return project_inputs(await self.connection.library.fetch(project_id, version))

    def prepare(self):
        if self.ticket and not self.ticket.finished:
            raise CloudError("RUN_UNCERTAIN")
        self.prepared = uuid.uuid4().hex
        self.ticket = None
        return self.prepared

    async def run(self, project_id, version, *, count=1, parallel=1, inputs=None, bindings=None, timeout=300):
        if not self.session:
            raise CloudError("AUTH_REQUIRED")
        bundle = await self.connection.library.fetch(project_id, version)
        inspection = project_inputs(bundle)
        raw = resolve_bindings(self.provider, bindings or {})
        values = {inspection["bindings"].get(k, k): v for k, v in raw.items()}
        required = set(bundle.manifest["required_secret_refs"])
        graph = __import__("poster.maker.model", fromlist=["Graph"]).Graph.model_validate_json(
            bundle.files[bundle.manifest["main"]]
        )
        secret_inputs = {name for name, v in graph.variables.items() if v.input and v.secret}
        if set(values) - required - secret_inputs:
            raise CloudError("SECRET_REQUIRED")
        self.ticket = await self.session.submit(
            project_id,
            version,
            intent=self.prepared,
            count=count,
            parallel=parallel,
            inputs=inputs,
            secrets={k: v for k, v in values.items() if k in secret_inputs},
            secret_refs={k: v for k, v in values.items() if k in required},
            timeout=timeout,
        )
        self.last = await self.session.wait(self.ticket, deadline=timeout * count + 30)
        return self.last

    async def cancel(self, index=None):
        if self.ticket:
            await self.session.cancel(self.ticket, index)

    async def close(self):
        if self.session:
            await self.session.close()
        if self.connection:
            await self.connection.close()


class JupyterLauncher:
    def __init__(self, backend):
        import ipywidgets as w

        self.backend, self.tasks = backend, set()
        self.projects, self.versions = w.Dropdown(description="Проект"), w.Dropdown(description="Версия")
        self.count = w.BoundedIntText(value=1, min=1, max=1000, description="Всего")
        self.parallel = w.BoundedIntText(value=1, min=1, max=32, description="Одновременно")
        self.inputs = w.Textarea(value="{}", description="Входы JSON")
        self.bindings = w.Textarea(value="{}", description="Имена secrets")
        self.status = w.HTML("Подключите Drive. Автономный режим DETACHED; Windows может быть выключен.")
        self.requirements = w.HTML("")
        self.connect_button = w.Button(description="Подключить Drive")
        self.run_button = w.Button(description="Выполнить", button_style="primary")
        self.prepare_button = w.Button(description="Подготовить новый запуск")
        self.cancel_button = w.Button(description="Отменить запуск")
        self.stop_button = w.Button(description="Завершить сессию")
        self.widget = w.VBox(
            [
                self.status,
                self.connect_button,
                self.projects,
                self.versions,
                self.requirements,
                self.count,
                self.parallel,
                w.HTML(
                    "Защищённые значения задайте в Secrets провайдера. Здесь — только соответствия ref/вход → имя секрета; значения не вводите."
                ),
                self.inputs,
                self.bindings,
                w.HBox([self.prepare_button, self.run_button, self.cancel_button, self.stop_button]),
            ]
        )
        self.connect_button.on_click(lambda _: self.spawn(self.connect()))
        self.run_button.on_click(lambda _: self.spawn(self.run()))
        self.prepare_button.on_click(lambda _: self.prepare())
        self.cancel_button.on_click(lambda _: self.spawn(backend.cancel()))
        self.stop_button.on_click(lambda _: self.spawn(backend.close()))
        self.projects.observe(
            lambda change: self.spawn(self.select(change["new"])) if change["new"] else None, names="value"
        )
        self.versions.observe(
            lambda change: self.spawn(self.inspect(change["new"])) if change["new"] else None, names="value"
        )

    def spawn(self, coroutine):
        async def safe():
            try:
                await coroutine
            except Exception as exc:
                self.status.value = (
                    exc.message
                    if isinstance(exc, CloudError)
                    else "Операция остановлена. Проверьте настройки; выполнение не повторялось."
                )

        task = asyncio.create_task(safe())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def connect(self):
        rows = await self.backend.connect()
        self.projects.options = [
            (p["name"] + " — " + p["project_id"][:8], p["project_id"]) for p in rows if p["status"] == "READY"
        ]
        self.parallel.max = self.backend.session.capacity.max_parallel
        self.status.value = (
            "Drive подключён. Максимум одновременно: " + str(self.parallel.max) + ". Режим DETACHED."
        )

    async def select(self, project_id):
        rows = await self.backend.versions(project_id)
        self.versions.options = [(p["version"][:12], p["version"]) for p in reversed(rows)]

    async def inspect(self, version):
        from html import escape

        value = await self.backend.inspect(self.projects.value, version)
        self.requirements.value = (
            "Входы: "
            + escape(", ".join(d["name"] + (" (защищён)" if d["protected"] else "") for d in value["inputs"]))
            + "<br>Защищённые bindings: "
            + escape(", ".join(value["bindings"]))
        )

    def prepare(self):
        try:
            self.backend.prepare()
            self.status.value = "Новый запуск подготовлен. Проверьте версию и входы."
        except CloudError as exc:
            self.status.value = exc.message

    async def run(self):
        self.run_button.disabled = True
        self.status.value = "EXECUTING — выполняется автономный запуск."
        try:
            inputs, bindings = json.loads(self.inputs.value), json.loads(self.bindings.value)
            if not isinstance(inputs, dict) or not isinstance(bindings, dict):
                raise CloudError("INPUT_INVALID")
            result = await self.backend.run(
                self.projects.value,
                self.versions.value,
                count=self.count.value,
                parallel=self.parallel.value,
                inputs=inputs,
                bindings=bindings,
            )
            self.status.value = "Результаты на Drive: " + ", ".join(r["status"] for r in result["runs"])
        finally:
            self.run_button.disabled = False


def jupyter(provider="generic", **kwargs):
    return JupyterLauncher(Launcher(provider, **kwargs))
