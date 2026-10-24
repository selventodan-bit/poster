"""Graph execution on Linux/Windows; intentionally imports no Tk or desktop modules."""

import asyncio
from dataclasses import asdict, dataclass, field
import time

from ..recorder.capture import chromium_options
from .actions import default_registry
from .model import GraphError
from .storage import Vault

ERRORS = {
    "ELEMENT_TIMEOUT": "Элемент или переход не дождался нужного состояния. Проверьте селектор и ожидание.",
    "BROWSER_ERROR": "Браузер не выполнил действие. Проверьте страницу, браузер и параметры кубика.",
    "SECRET_MISSING": "Защищённое значение отсутствует или зашифровано другим ключом. Задайте его заново.",
    "PASSWORD_REF_REQUIRED": "Для поля пароля нужна ссылка на защищённое значение.",
    "NO_CONNECTION": "У этого выхода нет связи. Соедините кубики на холсте.",
    "STEP_LIMIT": "Достигнут лимит переходов. Проверьте связи и циклы графа.",
    "PARAMETERS_INVALID": "Проверьте параметры выбранного кубика.",
    "ACTION_UNSUPPORTED": "Эта версия типа действия пока не поддерживается.",
    "URL_INVALID": "Нужен HTTP(S) URL без встроенного имени пользователя и пароля.",
    "ENTRY_MISSING": "Выберите кубик Start как точку входа.",
}


class BrowserSession:
    def __init__(self, headless=True):
        self.headless = headless
        self.playwright = self.browser = self.context = self.page = None

    async def start(self):
        from playwright.async_api import async_playwright

        starting = asyncio.create_task(async_playwright().start())
        try:
            self.playwright = await asyncio.shield(starting)
        except asyncio.CancelledError:
            self.playwright = await starting
            await self.close()
            raise
        try:
            self.browser = await self.playwright.chromium.launch(
                **chromium_options(self.headless, self.playwright.chromium.executable_path)
            )
            self.context = await self.browser.new_context()
            self.page = await self.context.new_page()
            return self
        except BaseException:
            await self.close()
            raise

    async def close(self):
        context, browser, driver = self.context, self.browser, self.playwright
        self.context = self.browser = self.playwright = self.page = None
        try:
            if context:
                await context.close()
        finally:
            try:
                if browser:
                    await browser.close()
            finally:
                if driver:
                    await driver.stop()


class Context:
    def __init__(self, vault, headless=True):
        self.vault = vault
        self.session = BrowserSession(headless)

    async def page(self):
        if self.session.page is None:
            await self.session.start()
        return self.session.page

    async def locator(self, params):
        root = await self.page()
        for frame in params.frame:
            root = root.frame_locator(frame)
        return root.locator(params.selector)


@dataclass
class NodeResult:
    node_id: str
    action: str
    status: str
    port: str = "success"
    value: dict = field(default_factory=dict)
    error_code: str | None = None
    message: str = ""
    elapsed_ms: int = 0


class GraphRunner:
    def __init__(self, graph, *, vault=None, registry=None, headless=True, on_event=None):
        self.graph = graph.validate_structure().model_copy(deep=True)
        self.registry = registry or default_registry()
        self.context = Context(vault or Vault(), headless)
        self.on_event = on_event or (lambda event: None)
        self.current = self.graph.entry
        self.state = "READY"
        self.results = []
        self.stopped = False
        self._action = None
        self._busy = False

    def event(self, **data):
        self.on_event(data)

    async def step(self):
        if self._busy:
            raise GraphError("RUN_BUSY")
        if self.state in {"SUCCEEDED", "FAILED", "STOPPED"}:
            return None
        self._busy = True
        started = time.monotonic()
        node = None
        try:
            if not self.current:
                raise GraphError("ENTRY_MISSING")
            node = self.graph.node(self.current)
            self.state = "RUNNING"
            self.event(kind="node", node_id=node.id, status="RUNNING")
            if len(self.results) >= self.graph.max_steps:
                raise GraphError("STEP_LIMIT")
            spec, params = self.registry.parameters(node)
            self._action = asyncio.create_task(spec.execute(self.context, params))
            port, value = await self._action
            result = NodeResult(node.id, node.type, "SUCCEEDED", port, value)
        except asyncio.CancelledError:
            self.stopped = True
            result = NodeResult(
                node.id if node else "",
                node.type if node else "",
                "CANCELLED",
                error_code="STOPPED",
                message="Остановлено. Уже выполненные действия не отменяются.",
            )
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, GraphError)
                else "ELEMENT_TIMEOUT"
                if type(exc).__name__ == "TimeoutError"
                else "BROWSER_ERROR"
            )
            result = NodeResult(
                node.id if node else "",
                node.type if node else "",
                "FAILED",
                "error",
                error_code=code,
                message=ERRORS.get(code, ERRORS["BROWSER_ERROR"]),
            )
        finally:
            self._action = None
            self._busy = False
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        self.results.append(result)
        if result.error_code == "STEP_LIMIT":
            self.state = "FAILED"
            self.current = None
        elif self.stopped:
            self.state = "STOPPED"
        elif node and node.type == "End" and result.status == "SUCCEEDED":
            self.state = "SUCCEEDED"
            self.current = None
        else:
            self.current = self.graph.successor(node.id, result.port) if node else None
            if self.current:
                self.state = "PAUSED"
            else:
                self.state = "FAILED"
                if not result.error_code:
                    result.error_code = "NO_CONNECTION"
                    result.status = "FAILED"
                    result.message = ERRORS["NO_CONNECTION"]
        self.event(kind="result", result=asdict(result), state=self.state, next_node=self.current)
        return result

    async def run(self):
        while self.state not in {"SUCCEEDED", "FAILED", "STOPPED"}:
            await self.step()
        return self.state

    async def stop(self):
        self.stopped = True
        if self._action and not self._action.done() and not self._action.cancelling():
            self._action.cancel()
        if self.state not in {"SUCCEEDED", "FAILED"}:
            self.state = "STOPPED"

    async def close(self):
        await self.stop()
        await self.context.session.close()
