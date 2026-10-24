"""Universal actions: real graphs and controlled HTTP without a browser."""

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import pytest
from poster.maker.model import Graph, Node, Edge
from poster.maker.runtime import GraphRunner, BrowserSession


def linear(*actions):
    nodes = [Node(id="start", type="Start")]
    for i, action in enumerate(actions):
        kind, params, *options = action
        nodes.append(Node(id="n" + str(i), type=kind, params=params, **(options[0] if options else {})))
    nodes.append(Node(id="end", type="End"))
    return Graph(
        entry="start",
        nodes=nodes,
        edges=[Edge(source=a.id, target=b.id) for a, b in zip(nodes, nodes[1:], strict=False)],
    )


@contextmanager
def api_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def request(self):
            self.server.calls.append((self.command, self.path))
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path.startswith("/slow"):
                time.sleep(0.15)
            status = (
                418 if self.path.startswith("/status") else 302 if self.path.startswith("/redirect") else 200
            )
            raw = (
                b"not json"
                if self.path.startswith("/invalid")
                else json.dumps(
                    {
                        "method": self.command,
                        "body": body.decode(errors="replace"),
                        "cookie": self.headers.get("Cookie", ""),
                        "header": self.headers.get("X-Test"),
                        "items": [1, 2, 3],
                        "flag": True,
                    }
                ).encode()
            )
            if self.path.startswith("/large"):
                raw = b"x" * 10000
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if self.path.startswith("/set"):
                self.send_header("Set-Cookie", "sid=private-cookie; Path=/")
            if status == 302:
                self.send_header("Location", "/echo")
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.calls = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Data action started a browser")

    monkeypatch.setattr(BrowserSession, "start", forbidden)


async def run_graph(graph, tmp_path, **kwargs):
    runner = GraphRunner(graph, workdir=tmp_path, **kwargs)
    try:
        await runner.run(breakpoints=False)
        return runner
    finally:
        await runner.close()


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
async def test_http_methods(method, tmp_path):
    with api_server() as (server, base):
        r = await run_graph(
            linear(
                (
                    "HTTP.Request",
                    {
                        "url": base + "/echo",
                        "method": method,
                        "protect_response": False,
                        "query": {"q": "Ж x"},
                        "headers": {"X-Test": "custom"},
                        "body_type": "json" if method != "HEAD" else "none",
                        "body": {"x": True},
                    },
                    {"save_as": "response"},
                )
            ),
            tmp_path,
        )
        assert r.state == "SUCCEEDED", r.results
        value = r.context.variables.reveal(r.context.variables.get("response"))
        assert value["status"] == 200
        if method != "HEAD":
            assert value["json"]["method"] == method and value["json"]["header"] == "custom"
        assert len(server.calls) == 1 and "q=" in server.calls[0][1]
        assert r.context.session.page is None


async def test_http_sessions_and_concurrency(tmp_path):
    with api_server() as (_, base):
        graph = linear(
            ("HTTP.SetHeader", {"name": "X-Test", "value": "shared"}),
            ("HTTP.SetCookie", {"name": "manual", "value": "value", "url": base, "secure": False}),
            ("HTTP.Get", {"url": base + "/set"}),
            ("HTTP.Get", {"url": base + "/echo"}, {"save_as": "with", "result_path": "json"}),
            ("HTTP.ClearCookies", {}),
            ("HTTP.Get", {"url": base + "/echo"}, {"save_as": "without", "result_path": "json"}),
        )
        a, b = await asyncio.gather(
            run_graph(graph, tmp_path / "one"),
            run_graph(
                linear(("HTTP.Get", {"url": base + "/echo"}, {"save_as": "value", "result_path": "json"})),
                tmp_path / "two",
            ),
        )
        assert a.state == b.state == "SUCCEEDED"
        v = a.context.variables
        got = v.reveal(v.get("with"))
        assert "sid=private-cookie" in got["cookie"] and "manual=value" in got["cookie"]
        assert not v.reveal(v.get("without"))["cookie"]
        assert not b.context.variables.reveal(b.context.variables.get("value"))["cookie"]
        assert "private-cookie" not in json.dumps([r.value for r in a.results])
        assert not a.context.http.client.cookies.jar


@pytest.mark.parametrize(
    "path,options,code",
    [
        ("/slow", {"timeout_ms": 20}, "HTTP_TIMEOUT"),
        ("/status", {}, "HTTP_STATUS"),
        ("/invalid", {"response_type": "json"}, "HTTP_DECODE"),
        ("/large", {"max_response_bytes": 100}, "HTTP_RESPONSE_LIMIT"),
    ],
)
async def test_http_errors_no_repeated_writes(path, options, code, tmp_path):
    with api_server() as (server, base):
        r = await run_graph(linear(("HTTP.Post", {"url": base + path, **options})), tmp_path)
        assert r.state == "FAILED" and r.results[-1].error_code == code, r.results
        assert len(server.calls) == 1


async def test_http_download_multipart_redirect_and_secret(tmp_path):
    from poster.maker.storage import Vault

    vault = Vault()
    with api_server() as (_, base):
        graph = linear(
            ("HTTP.Download", {"url": base + "/echo", "download_path": "Ж папка/data.bin"}),
            (
                "HTTP.Post",
                {
                    "url": base + "/echo",
                    "body_type": "multipart",
                    "body": {"text": "Ж"},
                    "files": {"file": "Ж папка/data.bin"},
                },
                {"save_as": "multipart", "result_path": "text"},
            ),
            ("HTTP.SetHeader", {"name": "Authorization", "value": vault.put("Bearer private-token")}),
            ("HTTP.Get", {"url": base + "/redirect"}, {"save_as": "secret"}),
        )
        r = await run_graph(graph, tmp_path, vault=vault)
        assert r.state == "SUCCEEDED", r.results
        assert (tmp_path / "Ж папка/data.bin").is_file()
        assert "private-token" not in str(r.results)
        assert "secret_ref" in r.context.variables.get("secret")
        assert "filename=" in r.context.variables.reveal(r.context.variables.get("multipart"))


async def test_http_connection_form_expected_status_and_request_cookies(tmp_path):
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    r = await run_graph(linear(("HTTP.Get", {"url": f"http://127.0.0.1:{port}/"})), tmp_path / "closed")
    assert r.results[-1].error_code == "HTTP_CONNECT"
    with api_server() as (_, base):
        r = await run_graph(
            linear(
                (
                    "HTTP.Post",
                    {
                        "url": base + "/echo",
                        "body_type": "form",
                        "body": {"name": "Ж x"},
                        "cookies": {"once": "private-once"},
                    },
                    {"save_as": "value", "result_path": "json"},
                ),
                (
                    "HTTP.Get",
                    {"url": base + "/status", "expected_status": []},
                    {"save_as": "status", "result_path": "status"},
                ),
            ),
            tmp_path / "form",
        )
        assert r.state == "SUCCEEDED", r.results
        value = r.context.variables.reveal(r.context.variables.get("value"))
        assert "name=" in value["body"] and "once=private-once" in value["cookie"]
        assert r.context.variables.reveal(r.context.variables.get("status")) == 418
        assert "private-once" not in str(r.results)
