"""Graph execution on Linux/Windows; intentionally imports no Tk or desktop modules."""

import asyncio
from dataclasses import asdict, dataclass, field
from collections import deque
from copy import deepcopy
import time
import json
from pathlib import Path

from .providers import make_provider
from .actions import default_registry
from .model import GraphError, secret_refs
from .navigation import matches, safe_url
from .variables import Variables, draft_parameters
from .storage import Vault
from .resources import Budget, FileSandbox, MAX_DATA
from .data_errors import ERRORS as DATA_ERRORS
from poster.portability.network import Network, ERRORS as NETWORK_ERRORS

ERRORS = {
    **NETWORK_ERRORS,
    "PROFILE_BUSY": "Профиль используется другим запуском. Остановите его перед изменением профиля.",
    "PROFILE_SCOPE": "Выберите разрешённые сайты для переноса браузерной сессии.",
    "PROFILE_INCOMPATIBLE": "Выбранная среда не поддерживает безопасный перенос этой сессии. Выберите чистую сессию.",
    "PROFILE_PERMISSION": "Разрешение переноса недействительно, истекло или выдано другой среде.",
    "PROFILE_LIMIT": "Разрешённые данные сессии превышают лимит переноса. Выберите меньше сайтов или чистую сессию.",
    **DATA_ERRORS,
    "POPUP_TIMEOUT": "После указанного действия новое окно не открылось. Проверьте кубик и политику popup сайта. Действие автоматически не повторялось.",
    "PROFILE_MISSING": "Этот локальный профиль отсутствует на компьютере. Выберите другой профиль или новую сессию.",
    "PROFILE_INVALID": "Профиль повреждён или содержит недопустимый путь. Остальные профили не изменены.",
    "TAB_CONTEXT_MISSING": "Записанная вкладка закрыта или ещё не создана. Проверьте NewTab/WaitPopup и связи графа.",
    "TAB_ID_DUPLICATE": "Две открытые вкладки получили одинаковый идентификатор. Проверьте граф.",
    "BROWSER_HOST_CLOSED": "Окно браузера закрыто. Остановите исполнение и откройте проект заново.",
    "BROWSER_HOST_START_FAILED": "Не удалось запустить встроенный браузер. Проверьте состав полной сборки и системные зависимости Qt.",
    "PROFILE_CONFLICT": "Сессия профиля была изменена другим запуском. Сохранение отменено, чтобы не потерять данные.",
    "WAIT_URL_TIMEOUT": "Переход не подтверждён. Сравните ожидаемую маску и фактический адрес в журнале. Форма повторно не отправлялась.",
    "WAIT_ANCHOR_MISSING": "Вызывающее переход действие не выполнялось в этой сессии. Проверьте связи графа.",
    "VARIABLE_MISSING": "Переменная ещё не задана. Проверьте порядок кубиков и имя переменной.",
    "VARIABLE_NAME_INVALID": "Имя переменной: латинские буквы, цифры и подчёркивание; первый символ не цифра.",
    "VARIABLE_TYPE_INVALID": "Тип значения не подходит для этой операции.",
    "VARIABLE_LIMIT": "Превышен лимит размера или количества переменных.",
    "INPUT_MISSING": "Заполните обязательные входные параметры проекта.",
    "INPUT_UNKNOWN": "Указан неизвестный входной параметр проекта.",
    "LOOP_LIMIT": "Превышен разрешённый лимит итераций цикла.",
    "LOOP_MISSING": "Break/Continue требует активного цикла.",
    "DEBUG_STATE_INVALID": "Сначала приостановите исполнение; структуру работающего графа менять нельзя.",
    "TAB_MISSING": "Вкладка с таким индексом не существует.",
    "PROJECT_FAILED": "Проект завершён кубиком неуспешного завершения.",
    "SELECTOR_TOO_BROAD": "Селектор нашёл больше 200 элементов. Уточните его перед действием.",
    "RESULT_PATH_INVALID": "Действие выполнено, но указанного поля результата нет. Исправьте путь результата; форма автоматически не повторялась.",
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
    def __init__(self, headless=True, *, provider="chromium", profile=None, network=None):
        self.headless = headless
        self.provider = make_provider(provider)
        self.profile = profile
        self.network = Network.parse(network)
        self.playwright = self.browser = self.context = self.page = None
        self.tabs = {}

    async def start(self):
        self.network.check(browser=True, provider=self.provider.name)
        from playwright.async_api import async_playwright

        starting = asyncio.create_task(async_playwright().start())
        try:
            self.playwright = await asyncio.shield(starting)
        except asyncio.CancelledError:
            self.playwright = await starting
            await self.close()
            raise
        try:
            self.record_binding = self.inspector_binding = False
            if self.network.kind != "direct":
                self.provider.network = self.network
            self.browser, self.context, self.page = await self.provider.start(self.playwright, self.headless)
            self.tabs = {"main": self.page}
            if self.profile:
                await self.profile.restore(self)
            return self
        except BaseException:
            await self.close()
            raise

    def bind_tab(self, name, page):
        if name in self.tabs and self.tabs[name] is not page and not self.tabs[name].is_closed():
            raise GraphError("TAB_ID_DUPLICATE")
        self.tabs[name] = page

    def tab_id(self, page):
        return next((name for name, item in self.tabs.items() if item is page), None)

    async def select_tab(self, name):
        page = self.tabs.get(name)
        if page is None or page.is_closed():
            raise GraphError("TAB_CONTEXT_MISSING")
        self.page = page
        await self.provider.activate(page)
        return page

    async def new_page(self, name=None):
        page = await self.provider.new_page()
        if name:
            self.bind_tab(name, page)
        self.page = page
        return page

    async def close(self):
        driver = self.playwright
        self.context = self.browser = self.playwright = self.page = None
        self.tabs = {}
        try:
            # Qt must not enter QApplication destruction while the Playwright
            # driver still owns CDP connections to its pages/inspector.
            if self.provider.name == "qt" and driver:
                context = getattr(self.provider, "context", None)
                for page in context.pages if context else []:
                    if not page.is_closed():
                        try:
                            await page.goto("about:blank", wait_until="load", timeout=3000)
                        except Exception:
                            pass  # Still close a page whose renderer has already stopped.
                browser = getattr(self.provider, "browser", None)
                if browser:
                    await browser.close()
                    self.provider.browser = None
                await driver.stop()
                driver = None
        finally:
            try:
                await self.provider.close()
            finally:
                if driver:
                    await driver.stop()


class Context:
    def __init__(
        self,
        vault,
        headless=True,
        *,
        definitions=None,
        inputs=None,
        provider="chromium",
        profile=None,
        network=None,
        workdir=None,
        allow_external_files=False,
    ):
        self.vault = vault
        self.variables = Variables(definitions or {}, vault, inputs)
        self.network = Network.parse(network)
        self.session = BrowserSession(headless, provider=provider, profile=profile, network=network)
        self.navigation_serial = 0
        self.action_serial = 0
        self.navigation_event = asyncio.Event()
        self.navigations = deque(maxlen=100)
        self.checkpoints = {}
        self.popup_checkpoints = {}
        self.last_checkpoint = None
        self.watched_pages = set()
        self.current_node = None
        self.sensitive_action = False
        self.loops = []
        self.control = None
        self.http = None
        self.files = FileSandbox(workdir, allow_external=allow_external_files)

    def use_page(self, page):
        self.session.page = page
        if page not in self.watched_pages:
            page.on("framenavigated", lambda frame: self.navigated(page, frame))
            self.watched_pages.add(page)
        return page

    async def page(self):
        if self.session.page is None:
            await self.session.start()
            self.use_page(self.session.page)
        if self.session.page.is_closed():
            raise GraphError("TAB_MISSING")
        return self.session.page

    def navigated(self, page, frame):
        if frame == page.main_frame:
            self.navigation_serial += 1
            self.navigations.append((self.navigation_serial, page, frame.url))
            self.navigation_event.set()

    async def before_action(self):
        page = await self.page()
        self.action_serial = self.navigation_serial
        self.last_checkpoint = (self.action_serial, page, page.url)
        if self.current_node:
            self.checkpoints[self.current_node] = self.last_checkpoint
            self.popup_checkpoints[self.current_node] = set(self.session.context.pages)
        return page

    async def wait_url(self, expected, timeout_ms, after_action, after_node):
        page = await self.page()
        checkpoint = self.checkpoints.get(after_node) if after_node else self.last_checkpoint
        if after_action and after_node and checkpoint is None:
            raise GraphError("WAIT_ANCHOR_MISSING")
        serial = checkpoint[0] if checkpoint else self.navigation_serial
        anchor_page = checkpoint[1] if checkpoint else page
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        observed = []
        reason = "no_navigation_after_action" if after_action else "url_mismatch"
        while True:
            observed = [
                url for number, owner, url in self.navigations if number > serial and owner == anchor_page
            ]
            historical = after_action and any(matches(expected, url) for url in observed)
            if (not after_action and matches(expected, page.url)) or historical:
                if matches(expected, page.url):
                    try:
                        remaining = max(1, int((deadline - asyncio.get_running_loop().time()) * 1000))
                        await page.wait_for_load_state("domcontentloaded", timeout=remaining)
                    except Exception:
                        reason = "domcontentloaded_timeout"
                        break
                return {
                    "matched": True,
                    "from_history": historical and not matches(expected, page.url),
                    "actual_url": safe_url(page.url),
                    "expected": safe_url(expected),
                }
            if observed:
                reason = "url_mismatch"
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            self.navigation_event.clear()
            try:
                await asyncio.wait_for(self.navigation_event.wait(), remaining)
            except TimeoutError:
                break
        raise GraphError(
            "WAIT_URL_TIMEOUT",
            details={
                "reason": reason,
                "reason_text": {
                    "no_navigation_after_action": "После указанного действия перехода не было. Проверьте, записана ли кнопка отправки.",
                    "url_mismatch": "Переход был, но его адрес не совпал с ожидаемой маской.",
                    "domcontentloaded_timeout": "Адрес совпал, но документ не завершил начальную загрузку.",
                }[reason],
                "actual_url": safe_url(page.url),
                "expected": safe_url(expected),
                "after_node": after_node,
                "observed": [safe_url(url) for url in observed[-5:]],
                "timeout_ms": timeout_ms,
                "form_retried": False,
            },
        )

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
    details: dict = field(default_factory=dict)
    isolated: bool = False


class GraphRunner:
    def __init__(
        self,
        graph,
        *,
        vault=None,
        registry=None,
        headless=True,
        on_event=None,
        inputs=None,
        provider="chromium",
        profile=None,
        network=None,
        workdir=None,
        allow_external_files=False,
        project_path=None,
        max_seconds=300,
        budget=None,
        ancestry=(),
        project_root=None,
    ):
        self.graph = graph.validate_structure().model_copy(deep=True)
        from poster.portability.network import policy

        if policy(self.graph.metadata.get("poster.network")) and network is None:
            raise GraphError("NETWORK_MISSING")
        Network.parse(network).check(
            browser=any(
                n.type.startswith("Browser.")
                or n.type == "Logic.If"
                and n.params.get("condition") != "equals"
                for n in self.graph.nodes
            ),
            provider=provider if isinstance(provider, str) else provider.name,
        )
        self.owns_budget = budget is None
        self.registry = registry or default_registry()
        self.context = Context(
            vault or Vault(),
            headless,
            definitions=self.graph.variables,
            inputs=inputs,
            provider=provider,
            profile=profile,
            network=network,
            workdir=workdir,
            allow_external_files=allow_external_files,
        )
        self.context.graph = self.graph
        self.context.budget = budget or Budget(self.graph.max_steps, max_seconds)
        self.context.files.budget = self.context.budget
        self.context.registry = self.registry
        self.context.project_path = Path(project_path).absolute().resolve() if project_path else None
        self.context.project_root = (
            Path(project_root).resolve()
            if project_root
            else (self.context.project_path.parent if self.context.project_path else self.context.files.root)
        )
        self.context.ancestry = (*ancestry, self.context.project_path or self.graph.id)
        self.on_event = on_event or (lambda event: None)
        self.context.emit = self.event
        self.current = self.graph.entry
        self.state = "READY"
        self.pause_reason = None
        self.results = []
        self.stopped = False
        self.pause_requested = False
        self._action = None
        self._busy = False

    def event(self, **data):
        self.on_event(data)

    def update_node(self, updated):
        if self._busy or self.state not in {"PAUSED", "SUCCEEDED", "FAILED"}:
            raise GraphError("DEBUG_STATE_INVALID")
        node = self.graph.node(updated.id)
        if (node.type, node.action_version) != (updated.type, updated.action_version):
            raise GraphError("DEBUG_STATE_INVALID")
        if (
            self.state == "PAUSED"
            and node.type in {"Logic.Switch", "Logic.Loop", "Logic.ForEach"}
            and node.params != updated.params
        ):
            raise GraphError("DEBUG_STATE_INVALID")
        draft_parameters(self.registry.get(updated.type, updated.action_version), updated.params)
        for key in (
            "params",
            "title",
            "breakpoint",
            "save_as",
            "result_path",
            "protect_result",
            "extensions",
        ):
            setattr(node, key, deepcopy(getattr(updated, key)))

    def update_graph(self, graph):
        if self._busy or graph.id != self.graph.id or self.state not in {"PAUSED", "SUCCEEDED", "FAILED"}:
            raise GraphError("DEBUG_STATE_INVALID")
        snapshot = graph.validate_structure().model_copy(deep=True)
        if self.state == "PAUSED":
            before = [(n.id, n.type, n.action_version) for n in self.graph.nodes]
            after = [(n.id, n.type, n.action_version) for n in snapshot.nodes]
            if before != after or snapshot.edges != self.graph.edges or snapshot.entry != self.graph.entry:
                raise GraphError("DEBUG_STATE_INVALID")
            for node in snapshot.nodes:
                current = self.graph.node(node.id)
                if (
                    current.type in {"Logic.Switch", "Logic.Loop", "Logic.ForEach"}
                    and current.params != node.params
                ):
                    raise GraphError("DEBUG_STATE_INVALID")
        self.graph = snapshot
        self.context.graph = snapshot

    def set_variable(self, name, value):
        if self._busy or self.state != "PAUSED":
            raise GraphError("DEBUG_STATE_INVALID")
        if secret_refs(self.context.variables.data.get(name)) and not secret_refs(value):
            value = self.context.vault.put_value(value)
        self.context.variables.set(name, value)
        self.event(kind="variables", values=self.context.variables.snapshot())

    async def execute(self, node, isolated=False):
        started = time.monotonic()
        self.context.current_node = node.id
        self.context.control = None
        spec = None
        self.event(kind="node", node_id=node.id, status="RUNNING")
        try:
            self.context.budget.consume()
            if len(self.results) >= self.graph.max_steps:
                raise GraphError("STEP_LIMIT")
            if node.type.startswith("Browser.") and node.extensions.get("browser_tab"):
                await self.context.page()
                self.context.use_page(await self.context.session.select_tab(node.extensions["browser_tab"]))
            values = self.context.variables.resolve(node.params)
            self.context.raw_params = values
            self.context.sensitive_action = node.protect_result or bool(secret_refs(values))
            spec = self.registry.get(node.type, node.action_version)
            spec, params = self.registry.parameters(
                node, self.context.variables.reveal(values) if spec.reveal_parameters else values
            )
            self._action = asyncio.create_task(spec.execute(self.context, params))
            try:
                port, value = await asyncio.wait_for(self._action, self.context.budget.remaining())
            except TimeoutError as exc:
                raise GraphError("RUN_TIMEOUT") from exc
            size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())
            self.context.budget.result_bytes += size
            if size > MAX_DATA or self.context.budget.result_bytes > 16_000_000:
                raise GraphError("DATA_LIMIT")
            if node.save_as:
                selected = value
                try:
                    for key in node.result_path.split(".") if node.result_path else ():
                        selected = selected[int(key)] if isinstance(selected, list) else selected[key]
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    raise GraphError("RESULT_PATH_INVALID") from exc
                if self.context.sensitive_action and not (
                    isinstance(selected, dict) and set(selected) == {"secret_ref"}
                ):
                    selected = self.context.vault.put_value(selected)
                self.context.variables.set(node.save_as, selected)
            visible = (
                {"protected": True} if self.context.sensitive_action else self.context.variables.public(value)
            )
            result = NodeResult(node.id, node.type, "SUCCEEDED", port, visible, isolated=isolated)
        except asyncio.CancelledError:
            self.stopped = True
            result = NodeResult(
                node.id,
                node.type,
                "CANCELLED",
                error_code="STOPPED",
                message="Остановлено. Уже выполненные действия не отменяются.",
                isolated=isolated,
            )
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, GraphError)
                else "FILE_OPERATION"
                if getattr(spec, "reveal_parameters", False)
                and isinstance(exc, (OSError, UnicodeError, LookupError))
                else "ELEMENT_TIMEOUT"
                if type(exc).__name__ == "TimeoutError"
                else "BROWSER_ERROR"
            )
            message = ERRORS.get(code, ERRORS["BROWSER_ERROR"])
            if code == "WAIT_URL_TIMEOUT":
                message += (
                    " "
                    + exc.details["reason_text"]
                    + " Ожидалось: "
                    + exc.details["expected"]
                    + "; фактически: "
                    + exc.details["actual_url"]
                )
            result = NodeResult(
                node.id,
                node.type,
                "FAILED",
                "error",
                error_code=code,
                message=message,
                details=exc.details if isinstance(exc, GraphError) else {},
                isolated=isolated,
            )
        finally:
            self._action = None
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        self.results.append(result)
        return result

    async def step(self):
        if self._busy:
            raise GraphError("RUN_BUSY")
        if self.state in {"SUCCEEDED", "FAILED", "STOPPED"}:
            return None
        if not self.current:
            self.state = "FAILED"
            self.event(kind="pause", reason="ENTRY_MISSING", node_id=None)
            return None
        self._busy = True
        node = self.graph.node(self.current)
        self.state = "RUNNING"
        try:
            result = await self.execute(node)
        finally:
            self._busy = False
        if result.error_code in {"STEP_LIMIT", "RUN_TIMEOUT", "RUN_STOPPED", "DATA_LIMIT"}:
            self.state, self.current = "FAILED", None
        elif self.stopped:
            self.state = "STOPPED"
        elif node.type in {"End", "End.Success", "End.Failure"} and result.status == "SUCCEEDED":
            self.state = "FAILED" if node.type == "End.Failure" else "SUCCEEDED"
            self.current = None
            if self.state == "FAILED":
                result.status, result.error_code = "FAILED", "PROJECT_FAILED"
                result.message = ERRORS["PROJECT_FAILED"]
        else:
            control = self.context.control if result.status == "SUCCEEDED" else None
            self.current = control["target"] if control else self.graph.successor(node.id, result.port)
            self.state = "PAUSED" if self.current else "FAILED"
            if self.current is None and not result.error_code:
                result.error_code, result.status = "NO_CONNECTION", "FAILED"
                result.message = ERRORS["NO_CONNECTION"]
        self.pause_reason = "step" if self.state == "PAUSED" else None
        self.event(kind="result", result=asdict(result), state=self.state, next_node=self.current)
        self.event(kind="variables", values=self.context.variables.snapshot())
        return result

    async def run(self, *, breakpoints=True):
        self.pause_requested = False
        skip_breakpoint = self.state == "PAUSED" and self.pause_reason == "breakpoint"
        while self.state not in {"SUCCEEDED", "FAILED", "STOPPED"}:
            if (
                breakpoints
                and self.current
                and self.graph.node(self.current).breakpoint
                and not skip_breakpoint
            ):
                self.state, self.pause_reason = "PAUSED", "breakpoint"
                self.event(kind="pause", reason="breakpoint", node_id=self.current)
                self.event(kind="variables", values=self.context.variables.snapshot())
                break
            skip_breakpoint = False
            await self.step()
            if self.pause_requested and self.state not in {"SUCCEEDED", "FAILED", "STOPPED"}:
                self.state, self.pause_reason = "PAUSED", "user"
                self.event(kind="pause", reason="user", node_id=self.current)
                break
        return self.state

    async def execute_selected(self, node_id):
        if self._busy or self.state not in {"PAUSED", "SUCCEEDED", "FAILED"}:
            raise GraphError("DEBUG_STATE_INVALID")
        node = self.graph.node(node_id)
        if node.type in {
            "Start",
            "End",
            "End.Success",
            "End.Failure",
            "Logic.Loop",
            "Logic.ForEach",
            "Logic.Break",
            "Logic.Continue",
        }:
            raise GraphError("DEBUG_STATE_INVALID")
        self._busy = True
        try:
            result = await self.execute(node, isolated=True)
        finally:
            self._busy = False
        self.context.control = None
        self.event(kind="result", result=asdict(result), state=self.state, next_node=self.current)
        self.event(kind="variables", values=self.context.variables.snapshot())
        return result

    def pause(self):
        self.pause_requested = True

    async def stop(self):
        self.stopped = True
        if self.owns_budget:
            self.context.budget.stopped = True
        if self._action and not self._action.done() and not self._action.cancelling():
            self._action.cancel()
        if self.state not in {"SUCCEEDED", "FAILED"}:
            self.state = "STOPPED"

    async def close(self):
        action = self._action
        await self.stop()
        if action and action is not asyncio.current_task() and not action.done():
            try:
                await action
            except asyncio.CancelledError:
                pass
        try:
            if self.context.http:
                await self.context.http.close()
        finally:
            await self.context.session.close()
