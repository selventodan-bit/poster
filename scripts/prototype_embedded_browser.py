"""Isolated, genuine QWebEngineView + Playwright/CDP compatibility experiment.

PySide6 is a prototype-only dependency, never imported by Poster or bundled in its ZIP.
"""

import argparse
import asyncio
import importlib.metadata
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import time
import traceback

from poster.maker.fixtures import serve_maker


def host(port, url, directory):
    os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = str(port)
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
        "--remote-debugging-address=127.0.0.1 --disable-gpu --no-sandbox"
    )
    os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
    os.environ["QT_QUICK_BACKEND"] = "software"
    os.environ["XDG_CACHE_HOME"] = str(directory / "cache")
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWidgets import QApplication, QMainWindow
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile

    app = QApplication([])
    window = QMainWindow()
    window.setWindowTitle("Poster embedded browser compatibility prototype")
    view = QWebEngineView(window)
    profile = QWebEngineProfile(view)
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
    view.setPage(QWebEnginePage(profile, view))
    window.setCentralWidget(view)
    window.resize(1000, 700)
    view.setUrl(QUrl(url))
    window.show()

    def poll():
        if (directory / "capture").exists():
            window.grab().save(str(directory / "embedded.png"))
            (directory / "capture").unlink()
        if (directory / "stop").exists():
            window.close()
            app.quit()

    timer = QTimer()
    timer.timeout.connect(poll)
    timer.start(80)
    app.exec()


async def check(directory):
    import httpx
    from playwright.async_api import async_playwright

    directory.mkdir(parents=True, exist_ok=False)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    with serve_maker() as (server, base), (directory / "host-private.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--host",
                "--port",
                str(port),
                "--url",
                base + "/",
                "--output",
                str(directory.resolve()),
            ],
            stdout=log,
            stderr=log,
        )
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                for _ in range(200):
                    if process.poll() is not None:
                        raise AssertionError("Embedded host exited before CDP became available")
                    try:
                        response = await client.get(f"http://127.0.0.1:{port}/json/version", timeout=1)
                        if response.status_code == 200:
                            version_data = response.json()
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError("Embedded CDP endpoint did not start")
            async with async_playwright() as playwright:
                default_connection = True
                try:
                    browser = await playwright.chromium.connect_over_cdp(
                        f"http://127.0.0.1:{port}", timeout=10000
                    )
                except Exception:
                    default_connection = False
                    browser = await playwright.chromium.connect_over_cdp(
                        f"http://127.0.0.1:{port}", timeout=15000, no_defaults=True
                    )
                context = browser.contexts[0]
                page = context.pages[0]
                captured = []
                await context.expose_binding(
                    "__prototype_action", lambda source, value: captured.append(value)
                )
                await context.add_init_script(
                    "document.addEventListener('click', e=>{if(e.isTrusted)window.__prototype_action({kind:'click',tag:e.target.tagName})},true)"
                )
                await page.goto(base + "/")
                await page.locator("#show").click()
                await page.locator("#note").fill("Embedded Qt + CDP Ж")
                await page.locator("#continue").click()
                await page.locator("#done").wait_for()
                assert server.posts["1"]["note"] == "Embedded Qt + CDP Ж"
                assert len(captured) >= 2
                new_context = "supported"
                try:
                    isolated = await asyncio.wait_for(browser.new_context(), 4)
                    await isolated.close()
                except Exception:
                    new_context = "unsupported"
                (directory / "capture").touch()
                for _ in range(50):
                    if (directory / "embedded.png").exists():
                        break
                    await asyncio.sleep(0.1)
                assert (directory / "embedded.png").is_file()
                user_agent = await page.evaluate("navigator.userAgent")
                chrome_version = re.search(r"Chrome/([^\s]+)", user_agent)
                browser_version = (
                    chrome_version[1] if chrome_version else version_data.get("Browser", browser.version)
                )
                await browser.close()
            sizes = {}
            for name in ["PySide6", "PySide6_Addons", "PySide6_Essentials", "shiboken6"]:
                distribution = importlib.metadata.distribution(name)
                sizes[name] = sum(
                    distribution.locate_file(f).stat().st_size
                    for f in distribution.files or []
                    if distribution.locate_file(f).is_file()
                )
            report = {
                "status": "PASSED",
                "platform": sys.platform,
                "qt_version": importlib.metadata.version("PySide6"),
                "browser_version": browser_version,
                "genuine_embedded_view": True,
                "setparent_used": False,
                "cdp": True,
                "dom_binding_and_navigation": True,
                "form_posts": len(server.posts),
                "isolated_context": new_context,
                "installed_bytes": sizes,
                "production_provider": False,
                "default_connection": default_connection,
            }
            (directory / "report.json").write_text(json.dumps(report, indent=2))
            print("::notice title=Embedded browser prototype::" + json.dumps(report), flush=True)
            return report
        finally:
            (directory / "stop").touch()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host", action="store_true")
    parser.add_argument("--url")
    parser.add_argument("--port", type=int)
    arguments = parser.parse_args()
    if arguments.host:
        host(arguments.port, arguments.url, arguments.output)
    else:
        started = time.monotonic()
        try:
            asyncio.run(check(arguments.output))
        except Exception as error:
            frames = " > ".join(
                f"{Path(frame.filename).name}:{frame.lineno}"
                for frame in traceback.extract_tb(error.__traceback__)[-4:]
            )
            print("::error::Embedded prototype " + type(error).__name__ + " at " + frames, flush=True)
            log = arguments.output / "host-private.log"
            if log.exists():
                for line in log.read_text(errors="replace").splitlines()[-12:]:
                    print(
                        "::notice title=Embedded host diagnostic::"
                        + re.sub(r"(?:https?|wss?)://\S+", "[local endpoint]", line),
                        flush=True,
                    )
            raise
