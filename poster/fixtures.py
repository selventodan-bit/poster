"""Two deliberately small, stateful test platforms. No production-site assumptions."""

import html
import json
import secrets
import threading
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        super().__init__(address, Handler)
        self.posts = {}
        self.tokens = {}
        self.lock = threading.Lock()
        self.token_history = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, code, body="", headers=None):
        raw = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in {"/classic/new", "/csrf/new"}:
            kind = path.split("/")[1]
            hidden, headers = "", {}
            if kind == "csrf":
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                sid = cookie["sid"].value if "sid" in cookie else secrets.token_hex(16)
                token = secrets.token_hex(24)
                with self.server.lock:
                    self.server.tokens[sid] = token
                    self.server.token_history.append(token)
                hidden = f'<input type="hidden" name="csrf_token" value="{token}">'
                headers["Set-Cookie"] = f"sid={sid}; HttpOnly; SameSite=Lax; Path=/"
            self.reply(
                200,
                f"""<!doctype html><meta charset="utf-8"><title>{kind} fixture</title>
                <form method="post" action="/{kind}/posts">{hidden}
                <label>Title <input name="subject" required></label>
                <label>Body <textarea name="message" required></textarea></label>
                <button type="submit">Publish</button></form>""",
                headers,
            )
        elif path.startswith("/posts/"):
            post_id = path.rsplit("/", 1)[-1]
            with self.server.lock:
                post = self.server.posts.get(post_id)
            if not post:
                self.reply(404, "Missing")
            else:
                self.reply(
                    200,
                    f'<article data-result-id="{post_id}"><h1>{html.escape(post["title"])}</h1>'
                    f'<div class="body">{html.escape(post["body"])}</div></article>',
                )
        elif path == "/api/posts":
            query = parse_qs(urlsplit(self.path).query)
            with self.server.lock:
                items = [
                    {"id": k, **v}
                    for k, v in self.server.posts.items()
                    if not query.get("title") or v["title"] == query["title"][0]
                ]
            self.reply(200, json.dumps({"items": items}, ensure_ascii=False))
        elif path == "/silent-success":
            self.reply(200, "OK")
        else:
            self.reply(404, "Missing")

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in {"/classic/posts", "/csrf/posts"}:
            self.reply(404, "Missing")
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            self.reply(413)
            return
        form = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
        if path.startswith("/csrf/"):
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            sid = cookie["sid"].value if "sid" in cookie else ""
            with self.server.lock:
                expected = self.server.tokens.get(sid)
                if not expected or form.get("csrf_token", [""])[0] != expected:
                    self.reply(403, "CSRF rejected")
                    return
                del self.server.tokens[sid]  # one use only
        title, body = form.get("subject", [""])[0], form.get("message", [""])[0]
        if not title or not body:
            self.reply(422, "Both fields required")
            return
        post_id = secrets.token_hex(8)
        with self.server.lock:
            self.server.posts[post_id] = {
                "title": title,
                "body": body,
                "site": "csrf" if path.startswith("/csrf") else "classic",
            }
        self.reply(303, headers={"Location": f"/posts/{post_id}"})


@contextmanager
def serve_fixture(host="127.0.0.1", port=0):
    server = FixtureServer((host, port))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://{host}:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def serve_forever(host="127.0.0.1", port=8765):
    print(f"Fixture sites on {host}:{port}: /classic/new and /csrf/new", flush=True)
    FixtureServer((host, port)).serve_forever()
