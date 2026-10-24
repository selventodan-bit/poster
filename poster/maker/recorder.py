"""Universal user recording. No CMS detector, request capture, routing or POST gate."""

import asyncio
import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from ..recorder.capture import Recorder
from .model import Edge, GraphError, Node, empty_graph
from .record_script import RECORD_JS, SELECTOR_EXPRESSION
from .runtime import BrowserSession
from .storage import Vault


def sensitive_url(url):
    parsed = urlsplit(url)
    return any(
        re.search(r"pass|token|secret|code|saml", k, re.I) for k, _ in parse_qsl(parsed.query)
    ) or bool(re.search(r"token=|secret=|password=", parsed.fragment, re.I))


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
    def __init__(self, *, headless=False, on_change=None, on_warning=None, vault=None):
        self.session = BrowserSession(headless)
        self.graph = empty_graph()
        self.vault = vault or Vault()
        self.on_change = on_change or (lambda graph: None)
        self.on_warning = on_warning or (lambda code: None)
        self.tail = self.graph.entry
        self.end = next(n.id for n in self.graph.nodes if n.type == "End")
        self.accepting = False
        self.pending = set()
        self.opening = 0
        self.renderer_navigation = False
        self.document_id = None
        self.last_input = None
        self.cdp = None

    def changed(self):
        self.on_change(self.graph.model_dump(mode="json"))

    def append(self, action, params):
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
        self.graph.nodes.insert(-1, node)
        self.tail = node.id
        end = self.graph.node(self.end)
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
        await self.session.start()
        try:
            # Reuse the existing Recorder's DOM binding mechanism, without its
            # network capture. Password POST bodies are never captured here.
            legacy = Recorder("about:blank", None)
            await legacy.attach_actions(
                self.session.context, self.action, script=RECORD_JS, binding="__poster_maker_action"
            )
            await self.session.page.evaluate(RECORD_JS)
            self.cdp = await self.session.context.new_cdp_session(self.session.page)
            await self.cdp.send("Page.enable")
            self.cdp.on(
                "Page.frameRequestedNavigation",
                lambda event: (
                    setattr(self, "renderer_navigation", True)
                    if event.get("reason")
                    in {
                        "anchorClick",
                        "formSubmissionGet",
                        "formSubmissionPost",
                        "scriptInitiated",
                        "metaTagRefresh",
                        "httpHeaderRefresh",
                    }
                    else None
                ),
            )
            self.session.page.on("framenavigated", self.navigation_event)
            self.accepting = True
            self.changed()
            return self
        except BaseException:
            await self.close()
            raise

    async def frame_path(self, frame):
        selectors = []
        while frame and frame.parent_frame:
            element = await frame.frame_element()
            selectors.insert(0, await element.evaluate(SELECTOR_EXPRESSION))
            frame = frame.parent_frame
        return selectors

    async def action(self, source, data):
        if not self.accepting:
            return
        if source["page"] != self.session.page:
            self.on_warning("POPUP_UNSUPPORTED")
            return
        try:
            kind = data.get("kind")
            selector = data.get("selector", "")
            if (
                kind not in {"click", "input", "select"}
                or not isinstance(selector, str)
                or len(selector) > 4000
            ):
                return
            frame = await self.frame_path(source["frame"])
            key = (tuple(frame), selector, data.get("document_id"))
            if not frame:
                self.document_id = data.get("document_id")
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
                    "Browser.Input", {**target, "value": value, "sensitive": bool(data.get("sensitive"))}
                )
                self.last_input = (key, node.id)
            elif kind == "select":
                self.append("Browser.Select", {**target, "values": data.get("values", [])})
                self.last_input = None
            else:
                self.append("Browser.Click", {**target, "button": data.get("button", "left")})
                self.last_input = None
        except Exception as exc:
            self.on_warning(exc.code if isinstance(exc, GraphError) else "RECORD_EVENT_FAILED")

    def navigation_event(self, frame):
        if not self.accepting or not self.session.page or frame != self.session.page.main_frame:
            return
        explicit = bool(self.opening)
        renderer = self.renderer_navigation
        self.renderer_navigation = False
        self.task(self.navigation(frame, explicit, renderer))

    async def navigation(self, frame, explicit, renderer):
        try:
            document = await frame.evaluate("window.__posterMakerDocumentId||null")
            same_document = bool(document and document == self.document_id)
            self.document_id = document
            if explicit or not self.accepting:
                return
            url = frame.url
            if urlsplit(url).scheme not in {"http", "https"}:
                return
            if renderer or same_document:
                self.append("Browser.WaitURL", {"url": navigation_pattern(url)})
            else:
                value = self.vault.put(url) if sensitive_url(url) else url
                self.append("Browser.OpenURL", {"url": value})
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

    async def stop(self):
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
        await self.session.close()
        self.changed()
        return self.graph

    async def close(self):
        self.accepting = False
        if self.pending:
            await asyncio.gather(*list(self.pending), return_exceptions=True)
        await self.session.close()
