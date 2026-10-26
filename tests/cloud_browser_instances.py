"""Five simultaneous Chromium contexts in real original Workers; explicit CI job."""

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from test_cloud_runtime import cloud, publish  # noqa: F401
from poster.cloud.runtime import NotebookSession
from poster.maker.model import Graph
from poster.cloud.package import PortableBundle
from poster.cloud.results import ResultArchive
from poster.dispatcher.store import Store


@contextmanager
def browser_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            with self.server.guard:
                self.server.calls.append(self.headers.get("Cookie", ""))
                n = len(self.server.calls)
            raw = f"<html><body>browser-{n}</body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Set-Cookie", f"instance={n}; Path=/")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.server.gate.wait(50)
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.calls = []
    server.guard = threading.Lock()
    server.gate = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.gate.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.asyncio
async def test_five_browser_sessions_have_independent_cookies_pids_and_outputs(cloud, tmp_path):  # noqa: F811
    lib, _, recipient = cloud
    with browser_server() as (server, base):
        bundle = await publish(
            cloud,
            tmp_path,
            [
                ("Browser.OpenURL", {"url": base + "/open"}),
                ("Browser.ReadText", {"selector": "body"}),
                ("Files.WriteText", {"path": "out.txt", "text": "{{page_text}}"}),
            ],
        )
        # Bind extraction through the same Graph model; no alternative executor.
        files = dict(bundle.files)
        graph = Graph.model_validate_json(files[bundle.manifest["main"]])
        extraction = next(n for n in graph.nodes if n.type == "Browser.ReadText")
        extraction.save_as = "page_text"
        extraction.result_path = "value"
        files[bundle.manifest["main"]] = graph.model_dump_json(indent=2).encode()
        from poster.cloud.format import digest

        manifest = bundle.manifest
        manifest["files"][manifest["main"]] = digest(files[manifest["main"]])
        updated = PortableBundle(manifest, files).verify()
        await lib.publish(
            updated, name="Five browsers", expected_version=bundle.version, recipient=recipient.public()
        )
        session = NotebookSession(lib, tmp_path / "browsers")
        try:
            assert session.capacity.browser and session.capacity.max_parallel >= 5, session.capacity
            ticket = await session.submit(
                updated.project_id, updated.version, intent="five-browser", count=5, parallel=5, timeout=90
            )
            for _ in range(1200):
                if len(server.calls) == 5:
                    break
                await asyncio.sleep(0.05)
            assert len(server.calls) == 5, await session.refresh(ticket)
            assert server.calls == [""] * 5  # no profile/cookie shared across instances
            state = await session.refresh(ticket)
            assert len({r["pid"] for r in state["runs"]}) == 5
            server.gate.set()
            state = await session.wait(ticket, deadline=60)
            assert all(r["status"] == "SUCCEEDED" for r in state["runs"]), state
            store = Store(tmp_path / "desktop" / "dispatcher.sqlite")
            try:
                archive = ResultArchive(store)
                results = await asyncio.gather(
                    *(archive.import_result(lib, ticket.claim, i, recipient) for i in range(5))
                )
                from pathlib import Path

                assert {(Path(r["directory"]) / "out.txt").read_text() for r in results} == {
                    f"browser-{i}" for i in range(1, 6)
                }
            finally:
                store.close()
        finally:
            server.gate.set()
            await session.close()
