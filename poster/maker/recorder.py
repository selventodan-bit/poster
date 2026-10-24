"""Universal user recording. No CMS detector, request capture, routing or POST gate."""

import asyncio
import re
import time
from urllib.parse import urlsplit, urlunsplit

from ..recorder.capture import Recorder
from .model import Edge, GraphError, Node, empty_graph
from .record_script import RECORD_JS, SELECTOR_EXPRESSION
from .runtime import BrowserSession
from .storage import Vault
from .navigation import sensitive_url


def navigation_pattern(url):
    parsed = urlsplit(url)

    def path(value):
        parts = value.split("/")
        return "/".join(
            "*" if re.fullmatch(r"\d+|[A-Fa-f0-9-]{16,}|[A-Za-z0-9_.~-]{30,}", p) else p.replace("*", "%2A")
            for p in parts
        )

    return urlunsplit((parsed.scheme, parsed.netloc, path(parsed.path), "", "")) + (
        "**#" + path(parsed.fragment) + "**" if parsed.fragment and not sensitive_url(url) else "**"
    )


class ActionRecorder:
    def __init__(
        self,
        *,
        headless=False,
        on_change=None,
        on_warning=None,
        vault=None,
        graph=None,
        after_node=None,
        session=None,
    ):
        self.session = session or BrowserSession(headless)
        self.graph = graph.validate_structure().model_copy(deep=True) if graph else empty_graph()
        self.vault = vault or Vault()
        self.on_change = on_change or (lambda graph: None)
        self.on_warning = on_warning or (lambda code: None)
        self.tail = after_node or self.graph.entry
        if self.graph.node(self.tail).type in {"End", "End.Success", "End.Failure"}:
            raise GraphError("INVALID_CONNECTION")
        self.end = self.graph.successor(self.tail, "success")
        if self.end is None:
            end = Node(type="End")
            self.graph.nodes.append(end)
            self.end = end.id
        self.accepting = False
        self.pending = set()
        self.opening = 0
        self.renderer_navigation = False
        self.renderer_intent_time = 0
        self.document_id = None
        self.last_input = None
        self.cdp = None
        self.last_action = None
        self.last_action_time = 0
        self.seen_events = set()
        self.pages = {}
        self.page_tasks = {}
        self.active_tab = "main"
        self.action_lock = asyncio.Lock()
        self.page_listener = lambda page: self.task(self.attach_page(page, created=True))

    def changed(self):
        self.on_change(self.graph.model_dump(mode="json"))

    def append(self, action, params, *, event_id=None, page=None):
        if len(self.graph.nodes) >= 2000:
            raise GraphError("RECORD_LIMIT")
        count = len(self.graph.nodes) - 1
        node = Node(type=action, params=params, x=50 + (count % 3) * 220, y=60 + (count // 3) * 125)
        self.graph.edges = [
            e for e in self.graph.edges if not (e.source == self.tail and e.port == "success")
        ]
        self.graph.edges.extend(
            [Edge(source=self.tail, target=node.id), Edge(source=node.id, target=self.end)]
        )
        if action.startswith("Browser."):
            tab = self.session.tab_id(page or self.session.page)
            if tab:
                node.extensions["browser_tab"] = tab
        self.graph.nodes.insert(-1, node)
        self.tail = node.id
        if event_id:
            node.extensions["record_event"] = event_id
        if action in {"Browser.Click", "Browser.Input", "Browser.Select", "Browser.OpenURL"}:
            self.last_action, self.last_action_time = node.id, time.monotonic()
        end = self.graph.node(self.end)
        if end.type in {"End", "End.Success", "End.Failure"}:
            count += 1
            end.x = 50 + (count % 3) * 220
            end.y = 60 + (count // 3) * 125
        self.changed()
        return node

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)
        return task

    async def start(self):
        if self.session.page is None:
            await self.session.start()
        try:
            # Reuse the existing Recorder's DOM binding mechanism, without its
            # network capture. Password POST bodies are never captured here.
            self.session.record_target = self
            if not getattr(self.session, "record_binding", False):
                legacy = Recorder("about:blank", None)
                session = self.session
                await legacy.attach_actions(
                    session.context,
                    lambda source, data: session.record_target.action(source, data),
                    script=RECORD_JS,
                    binding="__poster_maker_action",
                )
                session.record_binding = True
            self.session.context.on("page", self.page_listener)
            for page in self.session.context.pages:
                await self.attach_page(page)
            self.accepting = True
            self.changed()
            return self
        except BaseException:
            await self.close()
            raise

    async def attach_page(self, page, created=False):
        if page in self.pages:
            return
        self.pages[page] = {
            "cdp": None,
            "listener": None,
            "last_action": None,
            "renderer": False,
            "intent_time": 0,
            "ready": asyncio.Event(),
        }
        state = self.pages[page]
        try:
            if created:
                # The action binding may arrive before Playwright's page event.
                # Install the binding globally, then serialize creation before page actions.
                opener = await page.opener()
                if opener is None and self.session.provider.name == "qt":
                    provider = self.session.provider
                    host = await provider.request("state")
                    target = await provider.target(page)
                    parent = next((t["opener"] for t in host["tabs"] if t["target"] == target), None)
                    if parent:
                        opener = await provider.page_for(parent)
                name = "tab_" + str(len(self.session.tabs))
                self.session.bind_tab(name, page)
                if opener:
                    await asyncio.sleep(0.05)  # Drain the causal click binding before the popup.
                    cause = self.pages.get(opener, {}).get("last_action") or self.last_action
                    if not cause:
                        self.on_warning("POPUP_ANCHOR_MISSING")
                    else:
                        self.append("Browser.WaitPopup", {"tab_id": name, "after_node": cause}, page=opener)
                else:
                    self.append("Browser.NewTab", {"tab_id": name}, page=self.session.page)
                self.active_tab = name
                self.session.page = page
            for frame in page.frames:
                try:
                    await frame.evaluate(RECORD_JS)
                    await frame.evaluate("window.__posterMakerStart?.()")
                except Exception:
                    pass
            cdp = await self.session.context.new_cdp_session(page)
            state["cdp"] = cdp
            await cdp.send("Page.enable")
            main_id = (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
            cdp.on("Page.frameRequestedNavigation", lambda event: self.page_intent(page, event, main_id))

            def listener(frame):
                self.page_navigation(page, frame)

            state["listener"] = listener
            page.on("framenavigated", listener)
            state["close_listener"] = lambda: self.page_closed(page)
            page.on("close", state["close_listener"])
        except Exception:
            if not page.is_closed() and self.accepting:
                self.on_warning("TAB_ATTACH_FAILED")
        finally:
            state["ready"].set()

    def page_closed(self, page):
        if not self.accepting or not self.session.tab_id(page):
            return
        remaining = [p for p in self.session.context.pages if p is not page and not p.is_closed()]
        if remaining:
            self.append("Browser.CloseTab", {}, page=page)
            self.session.page = remaining[-1]
            self.active_tab = self.session.tab_id(self.session.page)

    def page_intent(self, page, event, main_id):
        if event.get("frameId") == main_id and event.get("reason") in {
            "anchorClick",
            "formSubmissionGet",
            "formSubmissionPost",
            "scriptInitiated",
            "metaTagRefresh",
            "httpHeaderRefresh",
        }:
            self.pages[page]["renderer"] = True
            self.pages[page]["intent_time"] = time.monotonic()

    def page_navigation(self, page, frame):
        if not self.accepting or frame != page.main_frame:
            return
        state = self.pages[page]
        renderer = state["renderer"] and time.monotonic() - state["intent_time"] < 2
        state["renderer"] = False
        self.task(self.navigation(frame, bool(self.opening), renderer, state["last_action"], page=page))

    def navigation_intent(self, event, main_id):
        if event.get("frameId") == main_id and event.get("reason") in {
            "anchorClick",
            "formSubmissionGet",
            "formSubmissionPost",
            "scriptInitiated",
            "metaTagRefresh",
            "httpHeaderRefresh",
        }:
            self.renderer_navigation = True
            self.renderer_intent_time = time.monotonic()

    async def frame_path(self, frame):
        selectors = []
        while frame and frame.parent_frame:
            element = await frame.frame_element()
            selectors.insert(0, await element.evaluate(SELECTOR_EXPRESSION))
            frame = frame.parent_frame
        return selectors

    async def action(self, source, data):
        async with self.action_lock:
            await self.record_action(source, data)

    async def record_action(self, source, data):
        if not self.accepting:
            return
        page = source["page"]
        if page not in self.pages:
            await self.attach_page(page, created=True)
        await self.pages[page]["ready"].wait()
        tab = self.session.tab_id(page)
        if tab != self.active_tab:
            self.append("Browser.SwitchTab", {"tab_id": tab}, page=self.session.page)
            self.active_tab = tab
        self.session.page = page
        try:
            kind = data.get("kind")
            selector = data.get("selector", "")
            if (
                kind not in {"click", "input", "select"}
                or not isinstance(selector, str)
                or len(selector) > 4000
            ):
                return
            event_id = str(data.get("document_id")) + ":" + str(data.get("sequence"))
            if data.get("sequence") is not None and event_id in self.seen_events:
                return
            self.seen_events.add(event_id)
            frame = await self.frame_path(source["frame"])
            key = (tab, tuple(frame), selector, data.get("document_id"))
            if not frame:
                self.document_id = data.get("document_id")
                self.pages[page]["document_id"] = self.document_id
            if (
                kind == "input"
                and self.last_input
                and self.last_input[0] == key
                and self.graph.node(self.tail).type == "Browser.Input"
            ):
                node = self.graph.node(self.tail)
                if data.get("sensitive") or node.params.get("sensitive"):
                    current = node.params.get("value", {})
                    node.params["value"] = self.vault.put(
                        data.get("value", ""),
                        current.get("secret_ref") if isinstance(current, dict) else None,
                    )
                    node.params["sensitive"] = True
                else:
                    node.params["value"] = str(data.get("value", ""))[:200000]
                self.changed()
                return
            target = {"selector": selector, "frame": frame}
            self.append("Browser.WaitElement", target)
            if kind == "input":
                value = (
                    self.vault.put(data.get("value", ""))
                    if data.get("sensitive")
                    else str(data.get("value", ""))[:200000]
                )
                node = self.append(
                    "Browser.Input",
                    {**target, "value": value, "sensitive": bool(data.get("sensitive"))},
                    event_id=event_id,
                )
                self.last_input = (key, node.id)
            elif kind == "select":
                self.append("Browser.Select", {**target, "values": data.get("values", [])}, event_id=event_id)
                self.last_input = None
            else:
                self.append(
                    "Browser.Click", {**target, "button": data.get("button", "left")}, event_id=event_id
                )
                self.last_input = None
            self.pages[page]["last_action"] = self.last_action
        except Exception as exc:
            self.on_warning(
                {
                    "code": exc.code if isinstance(exc, GraphError) else "RECORD_EVENT_FAILED",
                    "action": str(data.get("kind", ""))[:30],
                    "selector": str(data.get("selector", ""))[:400],
                }
            )

    def navigation_event(self, frame):
        if not self.accepting or not self.session.page or frame != self.session.page.main_frame:
            return
        explicit = bool(self.opening)
        renderer = self.renderer_navigation and time.monotonic() - self.renderer_intent_time < 2
        self.renderer_navigation = False
        self.task(self.navigation(frame, explicit, renderer, self.last_action))

    async def navigation(self, frame, explicit, renderer, after_node, page=None):
        try:
            document = await frame.evaluate("window.__posterMakerDocumentId||null")
            previous = self.pages.get(page, {}).get("document_id") if page else self.document_id
            same_document = bool(document and document == previous)
            self.document_id = document
            if page in self.pages:
                self.pages[page]["document_id"] = document
            if explicit or not self.accepting:
                return
            url = frame.url
            if urlsplit(url).scheme not in {"http", "https"}:
                return
            if same_document and not renderer:
                await asyncio.sleep(0.03)
                intent = await frame.evaluate("window.__posterMakerNavigationIntent||null")
                if intent and intent.get("url") == url:
                    event_id = str(document) + ":" + str(intent.get("cause"))
                    cause = next(
                        (n.id for n in self.graph.nodes if n.extensions.get("record_event") == event_id), None
                    )
                    if cause:
                        after_node, renderer = cause, True
            if after_node and renderer:
                if self.graph.node(self.tail).type == "Browser.WaitURL" and self.graph.node(
                    self.tail
                ).params.get("url") == navigation_pattern(url):
                    return
                self.append(
                    "Browser.WaitURL",
                    {"url": navigation_pattern(url), "after_action": True, "after_node": after_node},
                    page=page,
                )
            else:
                value = self.vault.put(url) if sensitive_url(url) else url
                self.append("Browser.OpenURL", {"url": value}, page=page)
            self.last_input = None
        except Exception:
            self.on_warning("NAVIGATION_NOT_RECORDED")

    async def navigate(self, url):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise GraphError("URL_INVALID")
        self.append("Browser.OpenURL", {"url": self.vault.put(url) if sensitive_url(url) else url})
        self.opening += 1
        try:
            await self.session.page.goto(url, wait_until="domcontentloaded")
        finally:
            self.opening -= 1

    async def stop(self, *, close=True):
        if self.session.context:
            for page in self.session.context.pages:
                for frame in page.frames:
                    try:
                        await frame.evaluate("window.__posterMakerStop?.()")
                    except Exception:
                        pass
        if self.pending:
            await asyncio.gather(*list(self.pending), return_exceptions=True)
        self.accepting = False
        if self.session.context:
            self.session.context.remove_listener("page", self.page_listener)
        for page, state in self.pages.items():
            if state["listener"]:
                page.remove_listener("framenavigated", state["listener"])
            if state.get("close_listener"):
                page.remove_listener("close", state["close_listener"])
            if state["cdp"]:
                try:
                    await state["cdp"].detach()
                except Exception:
                    pass
        self.pages = {}
        if close:
            await self.session.close()
        self.changed()
        return self.graph

    async def close(self):
        self.accepting = False
        if self.pending:
            await asyncio.gather(*list(self.pending), return_exceptions=True)
        await self.session.close()
