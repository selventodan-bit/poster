"""Playwright is intentionally imported only by the Recorder, never by the Engine."""

import asyncio
import os
import shutil
import time
import uuid
from email.message import Message
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass
class Capture:
    start_url: str
    title_marker: str = field(default_factory=lambda: "REC_TITLE_" + uuid.uuid4().hex[:12])
    body_marker: str = field(default_factory=lambda: "REC_BODY_" + uuid.uuid4().hex[:12])
    requests: list[dict] = field(default_factory=list)
    responses: list[dict] = field(default_factory=list)
    forms: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    session: dict = field(default_factory=dict)
    action_id: str | None = None
    result_url: str | None = None
    confirmation: dict = field(default_factory=dict)
    browser_verified: bool = False
    body_format: str = "text"
    publication_kind: str = "topic"

    @property
    def content_roles(self):
        return {"title", "body"} if self.publication_kind == "topic" else {"body"}


FORM_JS = """form => ({url: location.href,
  action: new URL(form.getAttribute('action')||location.href,form.baseURI).href,
  method: form.getAttribute('method')||'get',
  fields: [...form.elements].filter(e=>e.name).map(e=>({name:e.name, type:e.type,
    value: e.type === 'password' ? '' : e.value, selector: '[name='+JSON.stringify(e.name)+']'}))})"""
INIT_JS = """document.addEventListener('submit', e => {
 const f=e.target;
 if(f.tagName==='FORM') window.__poster_form((FORM_JS)(f));
}, true);
document.addEventListener('change', e => {
 const t=e.target; if(t.name) window.__poster_action({event:'change', name:t.name, type:t.type});
}, true);
document.addEventListener('input', e => {
 const t=e.target;
 if(!t.matches('input,textarea,[contenteditable="true"]') || ['password','hidden'].includes(t.type)) return;
 if(!t.hasAttribute('data-poster-observed')) t.setAttribute('data-poster-observed','field-'+(window.__posterFieldNo=(window.__posterFieldNo||0)+1));
 const scope=t.closest('form,[role="form"],section,article') || document;
 const inputs=[...scope.querySelectorAll('input')].filter(x=>['text',''].includes(x.type)&&x.getClientRects().length);
 window.__poster_action({event:'field-input',name:t.name||'',type:t.type||'',
   kind:t.isContentEditable?'contenteditable':t.tagName.toLowerCase(),
   value:t.isContentEditable?t.textContent:t.value,
   selector:'[data-poster-observed="'+t.getAttribute('data-poster-observed')+'"]',
   position:Math.max(0,inputs.indexOf(t))});
}, true);""".replace("FORM_JS", FORM_JS)


def chromium_options(headless=False, managed_executable=None):
    executable = os.environ.get("POSTER_CHROMIUM")
    if not executable and not (managed_executable and Path(managed_executable).is_file()):
        # Playwright's managed Chromium is preferred; system Chromium is a supported fallback.
        executable = shutil.which("chromium") or shutil.which("chromium-browser")
    options = {"headless": headless}
    if executable:
        options["executable_path"] = executable
    return options


class Recorder:
    def __init__(self, url: str, profile: Path):
        self.capture = Capture(url)
        self.profile = profile
        self.pending = set()
        self.request_records = {}
        self.response_event = asyncio.Event()
        self.browser_bodies = {}

    def task(self, coro):
        task = asyncio.create_task(coro)
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def attach(self, context):
        request_ids = {}
        request_actions = {}

        async def browser_body(source, identity, body):
            if (
                not isinstance(identity, str)
                or len(identity) > 80
                or not isinstance(body, str)
                or len(body) > 2_000_000
            ):
                return
            self.browser_bodies[identity] = body
            record = next((r for r in self.capture.requests if r.get("browser_id") == identity), None)
            if record:
                for response in self.capture.responses:
                    if response.get("request_id") == record["id"] and not response.get("body"):
                        response["body"] = body
                self.response_event.set()

        await context.expose_binding("__poster_response_body", browser_body)

        def request(req):
            if req.resource_type in {"document", "xhr", "fetch"}:
                request_id = f"r{len(self.capture.requests)}"
                request_ids[req] = request_id
                request_actions[req] = request_actions.get(req.redirected_from, self.capture.action_id)
                raw = req.post_data_buffer or b""
                content_type = req.headers.get("content-type", "")
                try:
                    if len(raw) > 2_000_000:
                        raise ValueError("Oversized capture")
                    mime = Message()
                    mime["content-type"] = content_type
                    body = raw.decode(mime.get_content_charset() or "utf-8")
                except (UnicodeError, ValueError, LookupError):
                    body, content_type = "", "application/octet-stream"
                self.capture.requests.append(
                    {
                        "id": request_id,
                        "browser_id": req.headers.get("x-poster-capture"),
                        "interaction": req.headers.get("x-poster-interaction"),
                        "action_id": request_actions[req],
                        "initiator": req.frame.url if req.frame else "",
                        "time": time.monotonic(),
                        "resource_type": req.resource_type,
                        "redirected_from": request_ids.get(req.redirected_from),
                        "method": req.method,
                        "url": req.url,
                        "body": body,
                        "content_type": content_type,
                        "headers": {
                            k: v
                            for k, v in req.headers.items()
                            if k.lower()
                            in {
                                "authorization",
                                "x-api-key",
                                "x-auth-token",
                                "x-csrf-token",
                                "x-xsrf-token",
                                "user-agent",
                            }
                        },
                    }
                )
                self.request_records[req] = self.capture.requests[-1]

        async def response(res):
            received_at = time.monotonic()
            action_id = request_actions.get(res.request)
            response_headers = dict(res.headers)
            if res.request.resource_type not in {"document", "xhr", "fetch"}:
                return
            try:
                try:
                    body = await res.text()
                except Exception:
                    body = ""
                body = body or self.browser_bodies.get(res.request.headers.get("x-poster-capture"), "")
                if len(body) > 2_000_000:
                    return
                self.capture.responses.append(
                    {
                        "request_id": request_ids.get(res.request),
                        "action_id": action_id,
                        "time": received_at,
                        "url": res.url,
                        "method": res.request.method,
                        "status": res.status,
                        "body": body,
                        "content_type": res.headers.get("content-type", ""),
                        "location": res.headers.get("location"),
                        "headers": response_headers,
                    }
                )
                self.response_event.set()
            except Exception:
                # Redirects and downloads need not have a readable body.
                pass

        await context.expose_binding("__poster_form", lambda _, data: self.capture.forms.append(data))

        def action(source, data):
            if len(self.capture.actions) >= 2000:
                self.capture.actions.pop(0)
            data["frame_url"] = source["frame"].url
            data["iframe"] = source["frame"] != source["page"].main_frame
            self.capture.actions.append(data)

        await context.expose_binding("__poster_action", action)
        await context.add_init_script(INIT_JS)
        context.on("request", request)
        context.on("response", lambda res: self.task(response(res)))

    async def finish(self, context):
        if self.pending:
            await asyncio.gather(*list(self.pending), return_exceptions=True)
        self.capture.session = await context.storage_state()
        # Tokens never appear in exported YAML or logs.
        for req in self.capture.requests:
            if req.get("headers") and urlsplit(req["url"]).netloc == urlsplit(self.capture.start_url).netloc:
                self.capture.session.setdefault("headers", {}).update(
                    {
                        k: v
                        for k, v in req["headers"].items()
                        if k in {"authorization", "x-api-key", "x-auth-token", "user-agent"}
                    }
                )
        await context.close()
        return self.capture

    async def record(self, stop_event, ready_callback, *, headless=False):
        from playwright.async_api import async_playwright

        self.profile.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                str(self.profile), **chromium_options(headless, p.chromium.executable_path)
            )
            await self.attach(context)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(self.capture.start_url)
            ready_callback(self.capture)
            await stop_event.wait()
            return await self.finish(context)

    async def automated_fixture(self, *, headless=True):
        """Real browser input/submit for reproducible E2E tests; identical capture hooks to the GUI."""
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                str(self.profile), **chromium_options(headless, p.chromium.executable_path)
            )
            await self.attach(context)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(self.capture.start_url)
            await page.locator('[name="subject"]').fill(self.capture.title_marker)
            await page.locator('[name="message"]').fill(self.capture.body_marker)
            await page.get_by_role("button", name="Publish").click()
            await page.locator("[data-result-id]").wait_for()
            return await self.finish(context)
