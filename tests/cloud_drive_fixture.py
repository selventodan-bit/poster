"""Controlled Drive v3 HTTP fixture. This is NOT a real Google Drive account E2E."""

from email import policy
from email.parser import BytesParser
import hashlib
import json
import re
import threading

import httpx

from poster.storage.drive import FOLDER


class DriveFixture:
    def __init__(self, page_size=2):
        self.page_size = page_size
        self.files, self.sessions, self.requests = {}, {}, []
        self.reserved, self.counter = set(), 0
        self.lock = threading.RLock()
        self.drop_chunk_once = False
        self.drop_claim_once = False
        self.corrupt_download = None
        self.quota_once = False

    def new_id(self):
        self.counter += 1
        value = f"drive_{self.counter:08d}"
        self.reserved.add(value)
        return value

    def info(self, file_id):
        value = self.files[file_id]
        raw = value["raw"]
        return {
            **value["meta"],
            "size": str(len(raw)),
            "sha256Checksum": hashlib.sha256(raw).hexdigest(),
            "trashed": False,
            "version": "1",
            "capabilities": {"canEdit": True},
        }

    def create(self, meta, raw=b""):
        file_id = meta["id"]
        if file_id in self.files:
            return httpx.Response(409, json={"error": {"code": 409}})
        assert file_id in self.reserved
        assert meta.get("mimeType")
        self.files[file_id] = dict(meta=meta, raw=raw)
        return httpx.Response(200, json=self.info(file_id))

    def __call__(self, req):
        with self.lock:
            return self.handle(req)

    def handle(self, req):
        assert req.headers.get("Authorization") == "Bearer DRIVE_FIXTURE_ONLY"
        self.requests.append((req.method, str(req.url)))
        if self.quota_once:
            self.quota_once = False
            return httpx.Response(
                429,
                json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}},
                headers={"Retry-After": "0"},
            )
        path = req.url.path
        if path.endswith("/files/generateIds"):
            return httpx.Response(
                200, json={"ids": [self.new_id() for _ in range(int(req.url.params["count"]))]}
            )
        if path == "/drive/v3/files" and req.method == "GET":
            query = req.url.params.get("q", "")
            parent = re.search(r"'([^']+)' in parents", query)
            properties = re.findall(r"properties has \{ key='([^']+)' and value='([^']+)' \}", query)
            rows = []
            for file_id, value in self.files.items():
                meta = value["meta"]
                if parent and parent.group(1) not in meta.get("parents", []):
                    continue
                if any(meta.get("properties", {}).get(k) != v for k, v in properties):
                    continue
                if "mimeType = '" + FOLDER + "'" in query and meta["mimeType"] != FOLDER:
                    continue
                if "mimeType != '" + FOLDER + "'" in query and meta["mimeType"] == FOLDER:
                    continue
                rows.append(self.info(file_id))
            start = int(req.url.params.get("pageToken", "0"))
            value = {"files": rows[start : start + self.page_size]}
            if start + self.page_size < len(rows):
                value["nextPageToken"] = str(start + self.page_size)
            return httpx.Response(200, json=value)
        if path == "/drive/v3/files" and req.method == "POST":
            return self.create(json.loads(req.content))
        if path.startswith("/drive/v3/files/"):
            file_id = path.rsplit("/", 1)[-1]
            if file_id not in self.files:
                return httpx.Response(404)
            if req.method == "PATCH":
                self.files[file_id]["meta"].update(json.loads(req.content))
                return httpx.Response(200, json=self.info(file_id))
            if req.url.params.get("alt") == "media":
                raw = self.files[file_id]["raw"]
                if self.corrupt_download == file_id:
                    raw = raw + b"CORRUPTED"
                return httpx.Response(200, content=raw)
            return httpx.Response(200, json=self.info(file_id))
        if path == "/upload/drive/v3/files" and req.method == "POST":
            if req.url.params.get("uploadType") == "multipart":
                parsed = BytesParser(policy=policy.default).parsebytes(
                    b"Content-Type: "
                    + req.headers["Content-Type"].encode()
                    + b"\r\nMIME-Version: 1.0\r\n\r\n"
                    + req.content
                )
                parts = list(parsed.iter_parts())
                meta = json.loads(parts[0].get_payload(decode=True))
                raw = parts[1].get_payload(decode=True)
                result = self.create(meta, raw)
                if self.drop_claim_once:
                    self.drop_claim_once = False
                    raise httpx.ReadError("fixture lost create acknowledgement", request=req)
                return result
            meta = json.loads(req.content)
            if meta["id"] in self.files:
                return httpx.Response(409)
            session_id = str(len(self.sessions) + 1)
            self.sessions[session_id] = dict(meta=meta, raw=b"")
            return httpx.Response(
                200,
                headers={
                    "Location": "https://www.googleapis.com/upload/drive/v3/files?upload_id=" + session_id
                },
            )
        if path == "/upload/drive/v3/files" and req.method == "PUT":
            session = self.sessions[req.url.params["upload_id"]]
            content_range = req.headers["Content-Range"]
            if content_range.startswith("bytes */"):
                total = int(content_range.split("/")[1])
                if len(session["raw"]) == total:
                    return httpx.Response(200, json=self.info(session["meta"]["id"]))
                return httpx.Response(
                    308,
                    headers={"Range": "bytes=0-" + str(len(session["raw"]) - 1)} if session["raw"] else {},
                )
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
            assert match
            start, end, total = map(int, match.groups())
            assert start == len(session["raw"]) and len(req.content) == end - start + 1
            session["raw"] += req.content
            if len(session["raw"]) == total:
                result = self.create(session["meta"], session["raw"])
            else:
                result = httpx.Response(308, headers={"Range": "bytes=0-" + str(len(session["raw"]) - 1)})
            if self.drop_chunk_once:
                self.drop_chunk_once = False
                raise httpx.ReadError("fixture lost chunk acknowledgement", request=req)
            return result
        raise AssertionError((req.method, req.url.path))


async def fixture_token():
    return "DRIVE_FIXTURE_ONLY"


def serve_drive_fixture():
    """Actual HTTP fixture for separate frozen processes; synthetic credentials only."""
    from contextlib import contextmanager
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    @contextmanager
    def serve():
        api = DriveFixture()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.dispatch()

            def do_POST(self):
                self.dispatch()

            def do_PUT(self):
                self.dispatch()

            def do_PATCH(self):
                self.dispatch()

            def dispatch(self):
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 25_000_000:
                    self.send_error(413)
                    return
                request = httpx.Request(
                    self.command,
                    "https://www.googleapis.com" + self.path,
                    headers=dict(self.headers),
                    content=self.rfile.read(length),
                )
                try:
                    response = api(request)
                except httpx.ReadError:
                    self.close_connection = True
                    return
                self.send_response(response.status_code)
                for key, value in response.headers.items():
                    if key.lower() not in {"content-length", "transfer-encoding", "connection"}:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True, name="drive-http-fixture")
        thread.start()
        try:
            yield api, "http://127.0.0.1:" + str(server.server_port)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    return serve()
