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


@pytest.mark.parametrize(
    "operation,params,expected",
    [
        ("Parse", {"text": '{"a":[true,null,3,{"x/y":false}]}'}, {"a": [True, None, 3, {"x/y": False}]}),
        ("Stringify", {"value": [True, None, 3]}, "[true, null, 3]"),
        ("Get", {"value": {"a/b": {"~": [False]}}, "path": "/a~1b/~0/0"}, False),
        ("Set", {"value": {"a": [1]}, "path": "/a/-", "new_value": None}, {"a": [1, None]}),
        ("Set", {"value": {"a": [1]}, "path": "/a/0", "new_value": True}, {"a": [True]}),
        ("Delete", {"value": {"a": [1, 2]}, "path": "/a/0"}, {"a": [2]}),
        ("Merge", {"left": {"a": {"x": 1}}, "right": {"a": {"y": False}}}, {"a": {"x": 1, "y": False}}),
    ],
)
async def test_json_operations(operation, params, expected, tmp_path):
    r = await run_graph(
        linear(("JSON." + operation, params, {"save_as": "out", "result_path": "value"})), tmp_path
    )
    assert r.state == "SUCCEEDED", r.results
    got = r.context.variables.get("out")
    assert got == expected and type(got) is type(expected)


@pytest.mark.parametrize(
    "operation,params,code",
    [
        ("Parse", {"text": "{"}, "JSON_INVALID"),
        ("Parse", {"text": "NaN"}, "JSON_INVALID"),
        ("Get", {"value": [], "path": "/0"}, "JSON_PATH_MISSING"),
        ("Get", {"value": {}, "path": "a"}, "JSON_PATH_INVALID"),
        ("Get", {"value": {}, "path": "/~2"}, "JSON_PATH_INVALID"),
        ("Delete", {"value": {}, "path": "/missing"}, "JSON_PATH_MISSING"),
    ],
)
async def test_json_errors(operation, params, code, tmp_path):
    r = await run_graph(linear(("JSON." + operation, params)), tmp_path)
    assert r.results[-1].error_code == code, r.results


@pytest.mark.parametrize(
    "operation,params,expected",
    [
        ("Create", {"items": [True, {"a": [None]}]}, [True, {"a": [None]}]),
        ("Add", {"items": [1], "item": False}, [1, False]),
        ("Insert", {"items": [1], "index": 0, "item": {"x": 2}}, [{"x": 2}, 1]),
        ("Get", {"items": [None, True], "index": 1}, True),
        ("Set", {"items": [0], "index": 0, "item": [1]}, [[1]]),
        ("Remove", {"items": [1, 2], "index": 0}, [2]),
        ("Count", {"items": [1, 2]}, 2),
        ("Contains", {"items": [True], "item": 1}, False),
        ("Clear", {"items": [1]}, []),
        ("Unique", {"items": [True, 1, True, {"x": 2}, {"x": 2}]}, [True, 1, {"x": 2}]),
        ("Shuffle", {"items": [1], "seed": 12}, [1]),
    ],
)
async def test_all_list_operations(operation, params, expected, tmp_path):
    r = await run_graph(
        linear(("Lists." + operation, params, {"save_as": "out", "result_path": "value"})), tmp_path
    )
    assert r.state == "SUCCEEDED", r.results
    got = r.context.variables.get("out")
    assert got == expected and type(got) is type(expected)


@pytest.mark.parametrize("operation", ["Get", "Set", "Insert", "Remove"])
async def test_list_bounds(operation, tmp_path):
    r = await run_graph(
        linear(
            (
                "Lists." + operation,
                {"items": [], "index": 2, **({"item": 1} if operation in {"Set", "Insert"} else {})},
            )
        ),
        tmp_path,
    )
    assert r.results[-1].error_code == "LIST_INDEX"


async def test_http_json_secret_list_foreach_http_without_browser(tmp_path):
    with api_server() as (server, base):
        graph = linear(
            ("HTTP.Get", {"url": base + "/echo"}, {"save_as": "raw", "result_path": "text"}),
            ("JSON.Parse", {"text": "{{raw}}"}, {"save_as": "data", "result_path": "value"}),
            (
                "JSON.Get",
                {"value": "{{data}}", "path": "/items"},
                {"save_as": "items", "result_path": "value"},
            ),
            ("Lists.Unique", {"items": "{{items}}"}, {"save_as": "items", "result_path": "value"}),
            ("Logic.ForEach", {"items": "{{items}}", "item": "number"}),
            ("HTTP.Post", {"url": base + "/echo", "body_type": "json", "body": "{{number}}"}),
            ("Logic.Continue", {}),
        )
        graph.edges.append(Edge(source="n4", port="error", target="end"))
        r = await run_graph(graph, tmp_path)
        assert r.state == "SUCCEEDED", r.results
        assert sum(method == "POST" for method, _ in server.calls) == 3
        assert r.context.variables.reveal(r.context.variables.get("number")) == 3
        assert r.context.variables.snapshot()["number"] == "••••"


async def test_new_action_breakpoint_edit_save_reopen(tmp_path):
    from poster.maker.storage import save_graph, load_graph, Vault

    graph = linear(
        ("Lists.Create", {"items": [1]}, {"save_as": "out", "result_path": "value", "breakpoint": True})
    )
    path = tmp_path / "saved.postergraph.json"
    save_graph(path, graph, Vault())
    graph, vault = load_graph(path)
    r = GraphRunner(graph, vault=vault, workdir=tmp_path)
    try:
        assert await r.run() == "PAUSED"
        node = r.graph.node("n0").model_copy(deep=True)
        node.params["items"] = [True, None, 3]
        r.update_node(node)
        assert await r.run() == "SUCCEEDED"
        assert r.context.variables.get("out") == [True, None, 3]
    finally:
        await r.close()


async def test_http_literal_credentials_encrypted_on_save_and_not_logged(tmp_path):
    from poster.maker.storage import save_graph, load_graph, Vault
    from poster.maker.variables import draft_parameters
    from poster.maker.actions import default_registry

    with api_server() as (_, base):
        graph = linear(
            (
                "HTTP.Post",
                {
                    "url": base + "/echo",
                    "body_type": "json",
                    "body": {"password": "PRIVATE_PASSWORD"},
                    "headers": {"Authorization": "Bearer PRIVATE_AUTH"},
                    "protect_response": False,
                },
                {"save_as": "reply"},
            )
        )
        path = tmp_path / "auth.postergraph.json"
        save_graph(path, graph, Vault())
        assert "PRIVATE_" not in path.read_text()
        graph, vault = load_graph(path)
        save_graph(path, graph, vault)
        graph, vault = load_graph(path)
        draft_parameters(default_registry().get("HTTP.Post"), graph.node("n0").params)
        r = await run_graph(graph, tmp_path, vault=vault)
        assert r.state == "SUCCEEDED", r.results
        assert "PRIVATE_" not in str(r.results) and r.context.variables.snapshot()["reply"] == "••••"


async def test_all_file_operations_unicode_and_bom(tmp_path):
    graph = linear(
        ("Files.CreateDirectory", {"path": "Ж папка"}),
        ("Files.WriteText", {"path": "Ж папка/a.txt", "text": "Привет", "encoding": "utf-8-sig"}),
        ("Files.AppendText", {"path": "Ж папка/a.txt", "text": "\n世界", "encoding": "utf-8-sig"}),
        (
            "Files.ReadText",
            {"path": "Ж папка/a.txt", "encoding": "utf-8-sig"},
            {"save_as": "text", "result_path": "value"},
        ),
        ("Files.Copy", {"source": "Ж папка/a.txt", "destination": "Ж папка/b.txt"}),
        ("Files.Move", {"source": "Ж папка/b.txt", "destination": "Ж папка/c.txt"}),
        ("Files.Exists", {"path": "Ж папка/b.txt"}, {"save_as": "exists", "result_path": "value"}),
        (
            "Files.ListDirectory",
            {"path": "Ж папка", "pattern": "*.txt"},
            {"save_as": "files", "result_path": "value"},
        ),
        ("Files.Delete", {"path": "Ж папка/c.txt"}),
    )
    r = await run_graph(graph, tmp_path)
    assert r.state == "SUCCEEDED", r.results
    assert r.context.variables.get("text") == "Привет\n世界"
    assert r.context.variables.get("exists") is False
    assert [x["name"] for x in r.context.variables.get("files")] == ["a.txt", "c.txt"]
    assert (tmp_path / "Ж папка/a.txt").read_bytes().count(b"\xef\xbb\xbf") == 1
    assert not (tmp_path / "Ж папка/c.txt").exists()


@pytest.mark.parametrize("path", ["../outside.txt", "a/../../outside.txt", "C:\\outside.txt"])
async def test_files_cannot_escape_workdir(path, tmp_path):
    r = await run_graph(linear(("Files.WriteText", {"path": path, "text": "wrong"})), tmp_path / "safe")
    assert r.results[-1].error_code == "FILE_OUTSIDE_WORKDIR"
    assert not (tmp_path / "outside.txt").exists()


async def test_file_links_overwrite_missing_and_concurrent_runs(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "valuable.txt").write_text("original")
    root = tmp_path / "safe"
    root.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pass
    else:
        r = await run_graph(linear(("Files.Delete", {"path": "link/valuable.txt"})), root)
        assert r.state == "FAILED" and (outside / "valuable.txt").read_text() == "original"
    graph = linear(("Files.WriteText", {"path": "same.txt", "text": "independent"}))
    a, b = await asyncio.gather(run_graph(graph, root / "a"), run_graph(graph, root / "b"))
    assert a.state == b.state == "SUCCEEDED"
    r = await run_graph(graph, root / "a")
    assert r.results[-1].error_code == "FILE_EXISTS"
    r = await run_graph(linear(("Files.ReadText", {"path": "missing"})), root)
    assert r.results[-1].error_code == "FILE_MISSING"
    r = await run_graph(
        linear(("Files.Move", {"source": "same.txt", "destination": "same.txt", "overwrite": True})),
        root / "a",
    )
    assert r.results[-1].error_code == "FILE_PATH_INVALID" and (root / "a/same.txt").exists()


async def test_default_runs_have_separate_files_and_explicit_external_permission(tmp_path):
    from poster.maker.resources import FileSandbox

    a, b = FileSandbox(), FileSandbox()
    assert a.root != b.root
    outside = tmp_path / "outside.txt"
    r = await run_graph(
        linear(("Files.WriteText", {"path": str(outside), "text": "explicit"})),
        tmp_path / "root",
        allow_external_files=True,
    )
    assert r.state == "SUCCEEDED" and outside.read_text() == "explicit"


TABLE = {"columns": ["name", "n"], "rows": [["a", 1], ["b", 2], ["c", 3]]}


@pytest.mark.parametrize(
    "operation,params,expected",
    [
        ("Create", TABLE, TABLE),
        (
            "AddRow",
            {"table": TABLE, "row": ["d", 4]},
            {"columns": ["name", "n"], "rows": [["a", 1], ["b", 2], ["c", 3], ["d", 4]]},
        ),
        ("GetCell", {"table": TABLE, "row": 1, "column": "n"}, 2),
        (
            "SetCell",
            {"table": TABLE, "row": 0, "column": "n", "value": False},
            {"columns": ["name", "n"], "rows": [["a", False], ["b", 2], ["c", 3]]},
        ),
        ("RemoveRow", {"table": TABLE, "row": 1}, {"columns": ["name", "n"], "rows": [["a", 1], ["c", 3]]}),
        ("FindRows", {"table": TABLE, "column": "n", "operator": "ge", "value": 2}, [1, 2]),
        (
            "Filter",
            {"table": TABLE, "column": "n", "operator": "gt", "value": 2},
            {"columns": ["name", "n"], "rows": [["c", 3]]},
        ),
        ("RowCount", {"table": TABLE}, 3),
        (
            "FromJSON",
            {"value": [{"a": True}, {"b": [None]}]},
            {"columns": ["a", "b"], "rows": [[True, None], [None, [None]]]},
        ),
        ("ToJSON", {"table": TABLE}, [{"name": "a", "n": 1}, {"name": "b", "n": 2}, {"name": "c", "n": 3}]),
    ],
)
async def test_each_table_operation(operation, params, expected, tmp_path):
    r = await run_graph(
        linear(("Tables." + operation, params, {"save_as": "out", "result_path": "value"})), tmp_path
    )
    assert r.state == "SUCCEEDED", r.results
    assert r.context.variables.get("out") == expected


@pytest.mark.parametrize("delimiter", [",", ";", "\t"])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
async def test_csv_quotes_newlines_empty_and_types(delimiter, encoding, tmp_path):
    table = {"columns": ["Ж", "data"], "rows": [['"quote",;\t\nline', ""], ["value", True]]}
    r = await run_graph(
        linear(
            (
                "CSV.Write",
                {"path": "данные/input.csv", "table": table, "delimiter": delimiter, "encoding": encoding},
            ),
            (
                "CSV.Read",
                {
                    "path": "данные/input.csv",
                    "delimiter": delimiter,
                    "encoding": encoding,
                    "infer_json": True,
                },
                {"save_as": "table", "result_path": "value"},
            ),
        ),
        tmp_path,
    )
    assert r.state == "SUCCEEDED", r.results
    assert r.context.variables.get("table") == table


async def test_excel_read_write_sheets_update_atomicity_formula_literals(tmp_path):
    import hashlib
    import openpyxl

    table = {
        "columns": ["label", "bool", "number", "empty"],
        "rows": [["=1+1", True, 3, None], ["Ж", False, 2.5, ""]],
    }
    graph = linear(
        ("Excel.Write", {"path": "Ж книга.xlsx", "table": table, "sheet": "Первый"}),
        ("Excel.Write", {"path": "Ж книга.xlsx", "table": TABLE, "sheet": "Второй", "update_existing": True}),
        ("Excel.ListSheets", {"path": "Ж книга.xlsx"}, {"save_as": "sheets", "result_path": "value"}),
        (
            "Excel.Read",
            {"path": "Ж книга.xlsx", "sheet": "Первый"},
            {"save_as": "table", "result_path": "value"},
        ),
    )
    r = await run_graph(graph, tmp_path)
    assert r.state == "SUCCEEDED", r.results
    assert r.context.variables.get("sheets") == ["Первый", "Второй"]
    got = r.context.variables.get("table")
    assert got["rows"][0] == ["=1+1", True, 3, None]
    book = openpyxl.load_workbook(tmp_path / "Ж книга.xlsx")
    assert book["Первый"]["A2"].data_type == "s"
    book.close()
    before = hashlib.sha256((tmp_path / "Ж книга.xlsx").read_bytes()).digest()
    r = await run_graph(
        linear(
            (
                "Excel.Write",
                {"path": "Ж книга.xlsx", "sheet": "Первый", "table": TABLE, "update_existing": True},
            )
        ),
        tmp_path,
    )
    assert r.state == "FAILED" and r.results[-1].error_code == "FILE_EXISTS"
    assert hashlib.sha256((tmp_path / "Ж книга.xlsx").read_bytes()).digest() == before
    r = await run_graph(
        linear(
            (
                "Excel.Write",
                {
                    "path": "Ж книга.xlsx",
                    "sheet": "Первый",
                    "table": TABLE,
                    "update_existing": True,
                    "replace_sheet": True,
                },
            ),
            (
                "Excel.Read",
                {"path": "Ж книга.xlsx", "sheet": "Первый"},
                {"save_as": "table", "result_path": "value"},
            ),
        ),
        tmp_path,
    )
    assert r.state == "SUCCEEDED" and r.context.variables.get("table") == TABLE


async def test_bad_table_csv_xlsx_and_empty_roundtrip(tmp_path):
    cases = [
        ("Tables.Create", {"columns": ["a", "a"], "rows": []}),
        ("Tables.GetCell", {"table": TABLE, "column": "missing", "row": 0}),
        ("CSV.Read", {"path": "bad.csv"}),
        ("Excel.Read", {"path": "bad.xlsx"}),
    ]
    (tmp_path / "bad.csv").write_text('a,b\n"unterminated', encoding="utf-8")
    (tmp_path / "bad.xlsx").write_bytes(b"bad")
    for action, params in cases:
        r = await run_graph(linear((action, params)), tmp_path)
        assert r.state == "FAILED", action
    r = await run_graph(
        linear(
            ("Excel.Write", {"path": "empty.xlsx", "table": {"columns": [], "rows": []}}),
            ("Excel.Read", {"path": "empty.xlsx"}, {"save_as": "empty", "result_path": "value"}),
        ),
        tmp_path,
    )
    assert r.state == "SUCCEEDED" and r.context.variables.get("empty") == {"columns": [], "rows": []}


async def test_xlsx_formula_cache_and_archive_limits(tmp_path):
    import openpyxl
    import zipfile

    book = openpyxl.Workbook()
    book.active.append(["formula"])
    book.active.append(["=1+1"])
    book.save(tmp_path / "formula.xlsx")
    book.close()
    r = await run_graph(
        linear(("Excel.Read", {"path": "formula.xlsx"}, {"save_as": "table", "result_path": "value"})),
        tmp_path,
    )
    assert r.state == "SUCCEEDED" and r.context.variables.get("table")["rows"] == [[None]]
    with zipfile.ZipFile(tmp_path / "bomb.xlsx", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("huge.xml", b"x" * 20_000_001)
    r = await run_graph(linear(("Excel.ListSheets", {"path": "bomb.xlsx"})), tmp_path)
    assert r.results[-1].error_code == "XLSX_INVALID"


def saved_child(path, *, secret=False, action=None):
    from poster.maker.model import Variable
    from poster.maker.storage import save_graph, Vault

    child = linear(
        action or ("Variables.Set", {"name": "result", "value": "{{argument}}"}),
        ("Variables.Set", {"name": "private_local", "value": "must not escape"}),
    )
    child.variables = {"argument": Variable(value=None, input=True, required=True, secret=secret)}
    child.extensions["outputs"] = ["result"]
    save_graph(path, child, Vault())
    return child


async def test_subproject_inputs_explicit_outputs_local_variables_and_protection(tmp_path):
    from poster.maker.storage import Vault, save_graph

    saved_child(tmp_path / "child.postergraph.json")
    parent = linear(
        (
            "Project.Run",
            {"path": "child.postergraph.json", "inputs": {"argument": [True, None, 3]}},
            {"save_as": "out", "result_path": "outputs"},
        ),
        ("Variables.Set", {"name": "after", "value": True}),
    )
    root = tmp_path / "parent.postergraph.json"
    save_graph(root, parent, Vault())
    r = await run_graph(parent, tmp_path / "work", project_path=root)
    assert r.state == "SUCCEEDED", r.results
    assert r.context.variables.get("out") == {"result": [True, None, 3]}
    assert "private_local" not in r.context.variables.data and r.context.variables.get("after") is True
    saved_child(tmp_path / "secret.postergraph.json", secret=True)
    vault = Vault()
    parent = linear(
        (
            "Project.Run",
            {"path": "secret.postergraph.json", "inputs": {"argument": vault.put_value([True, None])}},
            {"save_as": "out", "result_path": "outputs"},
        )
    )
    r = await run_graph(parent, tmp_path / "secret-work", project_path=root, vault=vault)
    assert r.state == "SUCCEEDED", r.results
    assert r.context.variables.reveal(r.context.variables.get("out")) == {"result": [True, None]}
    assert r.context.variables.snapshot()["out"] == "••••"


async def test_subproject_error_port_exports_and_recursion(tmp_path):
    from poster.maker.storage import save_graph, Vault

    child = linear(("JSON.Parse", {"text": "invalid"}))
    save_graph(tmp_path / "bad.json", child, Vault())
    parent = linear(("Project.Run", {"path": "bad.json"}))
    parent.edges.append(Edge(source="n0", port="error", target="end"))
    r = await run_graph(parent, tmp_path / "work", project_path=tmp_path / "root.json")
    assert r.state == "SUCCEEDED" and r.results[1].error_code == "PROJECT_CHILD_FAILED"
    assert r.results[1].details["child_code"] == "JSON_INVALID"
    root = tmp_path / "self.json"
    parent = linear(("Project.Run", {"path": "self.json"}))
    save_graph(root, parent, Vault())
    r = await run_graph(parent, tmp_path / "work", project_path=root)
    assert r.results[-1].error_code == "PROJECT_RECURSION"
    saved_child(tmp_path / "child.json")
    r = await run_graph(
        linear(
            ("Project.Run", {"path": "child.json", "inputs": {"argument": 1}, "outputs": ["private_local"]})
        ),
        tmp_path / "work",
        project_path=root,
    )
    assert r.results[-1].error_code == "PROJECT_OUTPUT_INVALID"


async def test_nested_projects_share_steps_time_and_depth_limits(tmp_path):
    from poster.maker.storage import save_graph, Vault

    saved_child(tmp_path / "child.json")
    parent = linear(("Project.Run", {"path": "child.json", "inputs": {"argument": 1}}))
    parent.max_steps = 4
    r = await run_graph(parent, tmp_path / "work", project_path=tmp_path / "root.json")
    assert r.state == "FAILED" and r.results[-1].error_code == "STEP_LIMIT"
    for i in range(10):
        save_graph(
            tmp_path / f"level{i}.json", linear(("Project.Run", {"path": f"level{i + 1}.json"})), Vault()
        )
    from poster.maker.storage import load_graph

    parent, vault = load_graph(tmp_path / "level0.json")
    r = await run_graph(parent, tmp_path / "work", project_path=tmp_path / "level0.json", vault=vault)
    assert r.state == "FAILED" and r.results[-1].error_code == "PROJECT_DEPTH"
    save_graph(tmp_path / "slow.json", linear(("Logic.Delay", {"milliseconds": 100})), Vault())
    r = await run_graph(
        linear(("Project.Run", {"path": "slow.json"})),
        tmp_path / "work",
        project_path=tmp_path / "root.json",
        max_seconds=0.02,
    )
    assert r.state == "FAILED" and r.results[-1].error_code == "RUN_TIMEOUT"


async def test_subproject_cancellation_closes_http_without_retry(tmp_path, monkeypatch):
    from poster.maker.storage import save_graph, Vault
    from poster.maker.http_actions import HTTPState

    closed = []
    original = HTTPState.close

    async def capture(self):
        await original(self)
        closed.append(self.client.is_closed)

    monkeypatch.setattr(HTTPState, "close", capture)
    with api_server() as (server, base):
        save_graph(tmp_path / "slow.json", linear(("HTTP.Post", {"url": base + "/slow"})), Vault())
        parent = GraphRunner(
            linear(("Project.Run", {"path": "slow.json"})),
            workdir=tmp_path / "work",
            project_path=tmp_path / "root.json",
        )
        task = asyncio.create_task(parent.run(breakpoints=False))
        try:
            for _ in range(100):
                if server.calls:
                    break
                await asyncio.sleep(0.01)
            assert server.calls
            await parent.stop()
            assert await asyncio.wait_for(task, 2) == "STOPPED"
            assert len(server.calls) == 1 and closed == [True]
        finally:
            await parent.close()
