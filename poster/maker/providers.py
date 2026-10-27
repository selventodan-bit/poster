"""GUI-independent browser providers. Qt is imported only in the owned host process."""

import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

from ..recorder.capture import chromium_options
from .model import GraphError


class ChromiumProvider:
    name = "chromium"

    async def start(self, driver, headless):
        options = chromium_options(headless, driver.chromium.executable_path)
        if getattr(self, "network", None):
            options["proxy"] = self.network.chromium()
        self.browser = await driver.chromium.launch(**options)
        self.context = await self.browser.new_context()
        return self.browser, self.context, await self.context.new_page()

    async def new_page(self):
        return await self.context.new_page()

    async def activate(self, page):
        await page.bring_to_front()

    async def close_page(self, page):
        await page.close()

    async def prepare_pointer(self, locator, timeout):
        pass

    async def close(self):
        if getattr(self, "browser", None):
            await self.browser.close()


class QtProvider:
    """Private QWebEngineProfile per process, JSON-lines over inherited pipes only.

    CDP is restricted to loopback; no executable code is accepted by the pipe API.
    Browser contexts/Target.createTarget are deliberately never used with Qt.
    """

    name = "qt"

    def __init__(self):
        self.process = self.browser = self.reader = None
        self.pending = {}
        self.serial = 0
        self.state = {}
        self.directory = None

    async def request(self, command, **params):
        if not self.process or self.process.returncode is not None:
            raise GraphError("BROWSER_HOST_CLOSED")
        self.serial += 1
        request_id = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            self.process.stdin.write(
                (json.dumps({"id": request_id, "command": command, "params": params}) + "\n").encode()
            )
            await self.process.stdin.drain()
            return await asyncio.wait_for(future, 10)
        finally:
            self.pending.pop(request_id, None)

    async def read_messages(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "state" in message:
                    self.state = message["state"]
                if message.get("event") == "closing":
                    self.process.stdin.close()
                future = self.pending.get(message.get("id"))
                if future and not future.done():
                    if message.get("error"):
                        future.set_exception(GraphError(message["error"]))
                    else:
                        future.set_result(message.get("result"))
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(GraphError("BROWSER_HOST_CLOSED"))

    async def start(self, driver, headless):
        # Headless Qt still needs a Qt display (Xvfb on Linux). CLI defaults to Chromium.
        self.directory = tempfile.TemporaryDirectory(prefix="poster-qt-")
        root = Path(self.directory.name)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        command = (
            [sys.executable, "--qt-browser-host"]
            if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "poster.maker.qt_host"]
        )
        env = os.environ.copy()
        env["POSTER_QT_PORT"] = str(port)
        env["POSTER_QT_TEMP"] = str(root)
        # A browser host never needs the encryption key, credentials or project secrets.
        env.pop("POSTER_SESSION_KEY", None)
        with (root / "host.log").open("wb") as log:
            self.process = await asyncio.create_subprocess_exec(
                *command,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=log,
                limit=2_000_000,
            )
        self.reader = asyncio.create_task(self.read_messages())
        import httpx

        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(150):
                if self.process.returncode is not None:
                    raise GraphError("BROWSER_HOST_START_FAILED")
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/json/version", timeout=0.5)
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise GraphError("BROWSER_HOST_START_FAILED")
        self.browser = await driver.chromium.connect_over_cdp(
            f"http://127.0.0.1:{port}", timeout=15000, no_defaults=True
        )
        self.context = self.browser.contexts[0]
        await self.request("state")
        return self.browser, self.context, self.context.pages[0]

    async def target(self, page):
        cdp = await self.context.new_cdp_session(page)
        try:
            return (await cdp.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
        finally:
            await cdp.detach()

    async def page_for(self, target):
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            for page in self.context.pages:
                if await self.target(page) == target:
                    return page
            await asyncio.sleep(0.05)
        raise GraphError("TAB_MISSING")

    async def new_page(self):
        page = await self.page_for(await self.request("new_tab"))
        await self.activate(page)
        return page

    async def activate(self, page):
        await self.request("activate", target=await self.target(page))

    async def close_page(self, page):
        await self.request("close_tab", target=await self.target(page))
        if not page.is_closed():
            await page.wait_for_event("close", timeout=3000)

    async def prepare_pointer(self, locator, timeout):
        # Qt's compositor can lag the CDP scroll acknowledgement in a clipped
        # iframe. Wait for the owning document's paint frames before dispatch.
        await locator.scroll_into_view_if_needed(timeout=timeout)
        await asyncio.wait_for(
            locator.evaluate(
                "e=>new Promise(resolve=>{const w=e.ownerDocument.defaultView; w.requestAnimationFrame(()=>w.requestAnimationFrame(resolve))})"
            ),
            timeout / 1000,
        )

        await locator.page.screenshot(timeout=timeout)  # Qt compositor read barrier; bytes stay in memory.

    async def active_page(self):
        state = await self.request("state")
        return await self.page_for(state["active"]) if state.get("active") else None

    async def close(self):
        try:
            if self.browser:
                await self.browser.close()  # Disconnect CDP; Qt owns the real browser.
            if self.process and self.process.returncode is None:
                try:
                    await self.request("quit")
                except Exception:
                    pass
                self.process.stdin.close()
                try:
                    await self.process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                try:
                    await asyncio.wait_for(self.process.wait(), 8)
                except TimeoutError:
                    self.process.kill()  # Only this provider's child, never an installed Chrome.
                    await self.process.wait()
        finally:
            if self.reader:
                await asyncio.gather(self.reader, return_exceptions=True)
            if self.directory:
                self.directory.cleanup()


def make_provider(name):
    if not isinstance(name, str):
        return name
    if name == "chromium":
        return ChromiumProvider()
    if name == "qt":
        return QtProvider()
    raise GraphError("BROWSER_PROVIDER_UNKNOWN")
