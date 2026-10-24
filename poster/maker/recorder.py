"""Universal user recording. No CMS detector, request capture, routing or POST gate."""

import asyncio
import re
import time
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

    def changed(self):
        self.on_change(self.graph.model_dump(mode="json"))

    def append(self, action, params, *, event_id=None):
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
            for frame in self.session.page.frames:
                await frame.evaluate(RECORD_JS)
                await frame.evaluate("window.__posterMakerStart?.()")
            self.cdp = await self.session.context.new_cdp_session(self.session.page)
            await self.cdp.send("Page.enable")
            main_id = (await self.cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
            self.cdp.on(
                "Page.frameRequestedNavigation",
                lambda event: self.navigation_intent(event, main_id),
            )
            self.session.page.on("framenavigated", self.navigation_event)
            self.accepting = True
            self.changed()
            return self
        except BaseException:
            await self.close()
            raise

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
            event_id = str(data.get("document_id")) + ":" + str(data.get("sequence"))
            if data.get("sequence") is not None and event_id in self.seen_events:
                return
            self.seen_events.add(event_id)
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

    async def navigation(self, frame, explicit, renderer, after_node):
        try:
            document = await frame.evaluate("window.__posterMakerDocumentId||null")
            same_document = bool(document and document == self.document_id)
            self.document_id = document
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
                )
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
        if self.session.page:
            self.session.page.remove_listener("framenavigated", self.navigation_event)
        if self.cdp:
            await self.cdp.detach()
            self.cdp = None
        if close:
            await self.session.close()
        self.changed()
        return self.graph

    async def close(self):
        self.accepting = False
        if self.pending:
            await asyncio.gather(*list(self.pending), return_exceptions=True)
        await self.session.close()
