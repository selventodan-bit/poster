"""Playwright is intentionally imported only by the Recorder, never by the Engine."""

import asyncio
import json
import os
import shutil
import uuid
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


FORM_JS = """form => ({url: location.href, action: form.action, method: form.method,
  fields: [...form.elements].filter(e=>e.name).map(e=>({name:e.name, type:e.type,
    value: e.type === 'password' ? '' : e.value, selector: '[name='+JSON.stringify(e.name)+']'}))})"""
INIT_JS = """document.addEventListener('submit', e => {
 const f=e.target;
 if(f.tagName==='FORM') window.__poster_form((FORM_JS)(f));
}, true);
document.addEventListener('change', e => {
 const t=e.target; if(t.name) window.__poster_action({event:'change', name:t.name, type:t.type});
}, true);""".replace("FORM_JS", FORM_JS)


def marker_shortcut_script(capture):
    """Browser-side typing of capture markers without the OS clipboard."""
    markers = json.dumps({"Digit1": capture.title_marker, "Digit2": capture.body_marker})
    script = """
    (() => {
      const markers = __POSTER_MARKERS__;
      document.addEventListener('keydown', (event) => {
        if (!event.ctrlKey || !event.altKey || event.repeat) return;
        const marker = markers[event.code];
        if (!marker) return;
        const field = document.activeElement;
        if (!field) return;
        const writable = field.isContentEditable ||
          field.matches('textarea, input:not([type="hidden"]):not([type="password"])');
        if (!writable || field.disabled || field.readOnly) return;
        event.preventDefault();
        const inserted = document.execCommand('insertText', false, marker);
        if (!inserted && typeof field.setRangeText === 'function') {
          field.setRangeText(marker, field.selectionStart, field.selectionEnd, 'end');
          field.dispatchEvent(new Event('input', {bubbles: true}));
        }
      }, true);
    })();
    """
    return script.replace("__POSTER_MARKERS__", markers)


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

    def task(self, coro):
        task = asyncio.create_task(coro)
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def attach(self, context):
        def request(req):
            if req.resource_type in {"document", "xhr", "fetch"}:
                self.capture.requests.append(
                    {
                        "method": req.method,
                        "url": req.url,
                        "body": req.post_data or "",
                        "content_type": req.headers.get("content-type", ""),
                        "headers": {
                            k: v
                            for k, v in req.headers.items()
                            if k.lower() in {"authorization", "x-api-key"}
                        },
                    }
                )

        async def response(res):
            if res.request.resource_type not in {"document", "xhr", "fetch"}:
                return
            try:
                body = await res.text()
                if len(body) > 2_000_000:
                    return
                self.capture.responses.append(
                    {
                        "url": res.url,
                        "method": res.request.method,
                        "status": res.status,
                        "body": body,
                        "content_type": res.headers.get("content-type", ""),
                    }
                )
            except Exception:
                # Redirects and downloads need not have a readable body.
                pass

        await context.expose_binding("__poster_form", lambda _, data: self.capture.forms.append(data))
        await context.expose_binding("__poster_action", lambda _, data: self.capture.actions.append(data))
        await context.add_init_script(INIT_JS)
        await context.add_init_script(marker_shortcut_script(self.capture))
        context.on("request", request)
        context.on("response", lambda res: self.task(response(res)))

    async def finish(self, context):
        if self.pending:
            await asyncio.gather(*list(self.pending), return_exceptions=True)
        self.capture.session = await context.storage_state()
        # Tokens never appear in exported YAML or logs.
        for req in self.capture.requests:
            if req.get("headers") and urlsplit(req["url"]).netloc == urlsplit(self.capture.start_url).netloc:
                self.capture.session.setdefault("headers", {}).update(req["headers"])
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
