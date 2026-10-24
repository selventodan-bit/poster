"""Controlled browser recording site, including ordinary POST/CSRF authorization."""

from contextlib import contextmanager
import html
import json
from http.cookies import SimpleCookie
import secrets
import threading
from urllib.parse import parse_qs, urlsplit

from ..fixtures import FixtureServer, Handler

DEMO_PASSWORD = "demo-password-Ж42"


class MakerHandler(Handler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/data":
            raw = json.dumps(
                {"items": [{"name": "alpha", "value": 3}, {"name": "beta", "value": 5}], "ok": True}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif path == "/":
            self.reply(
                200,
                """<!doctype html><meta charset="utf-8"><title>ProjectMaker demo</title>
            <h1>Демо ProjectMaker</h1><button id="show" onclick="document.getElementById('form').hidden=false">Показать форму</button>
            <form id="form" hidden method="post" action="/save"><label>Текст <input id="note" name="note"></label>
            <label>Режим <select id="mode" name="mode"><option value="one">Один</option><option value="two">Два</option></select></label>
            <button id="continue" type="submit">Перейти дальше</button></form><p><a href="/login">Проверка входа</a></p>""",
            )
        elif path == "/async":
            self.reply(
                200,
                """<meta charset="utf-8"><title>ProjectMaker async</title><button id="async" onclick="setTimeout(()=>location.href='/async?stage=two',250)">Next</button>""",
            )
        elif path == "/tabs":
            self.reply(
                200,
                '<meta charset="utf-8"><title>Maker tabs</title><a id="popup" target="_blank" href="/wide-iframe">Открыть форму</a><input id="main-note"><select id="choice"><option value="a">A</option><option value="b">B</option></select>',
            )
        elif path == "/wide-iframe":
            self.reply(
                200,
                '<meta charset="utf-8"><title>ProjectMaker iframe</title><iframe id="inner" src="/login" width="700" height="400"></iframe>',
            )
        elif path == "/iframe":
            self.reply(
                200,
                '<meta charset="utf-8"><title>ProjectMaker iframe</title><iframe id="inner" src="/login"></iframe>',
            )
        elif path == "/after-nav":
            self.reply(
                200,
                '<meta charset="utf-8"><title>ProjectMaker navigation</title><a id="move" href="/after-nav?stage=two">Next</a><input id="late">',
            )
        elif path == "/spa":
            self.reply(
                200,
                '<meta charset="utf-8"><title>ProjectMaker SPA</title><input id="spa-input"><button id="route" onclick="history.pushState({},\'\',\'/spa?step=two\')">Route</button><a id="hash" href="#part">Hash</a>',
            )
        elif path.startswith("/result/"):
            item = self.server.posts.get(path.rsplit("/", 1)[-1])
            self.reply(
                200,
                '<meta charset="utf-8"><title>ProjectMaker result</title><h1 id="done">Готово</h1><output id="result">'
                + html.escape(item["note"])
                + "</output>"
                if item
                else "Missing",
            )
        elif path == "/login":
            sid = secrets.token_hex(12)
            token = secrets.token_hex(16)
            self.server.tokens[sid] = token
            self.reply(
                200,
                f'''<meta charset="utf-8"><h1>Контролируемый вход</h1>
            <form method="post" action="/login"><input type="hidden" name="csrf" value="{token}">
            <input id="user" name="user" autocomplete="username"><input id="password" name="password" type="password" autocomplete="current-password">
            <button id="login" type="submit">Войти</button></form>''',
                {"Set-Cookie": f"sid={sid}; HttpOnly; SameSite=Lax; Path=/"},
            )
        elif path == "/account":
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            sid = cookies["sid"].value if "sid" in cookies else ""
            if sid not in self.server.authorized:
                self.reply(401, "Login required")
                return
            self.reply(
                200,
                '<meta charset="utf-8"><title>Maker authorized</title><h1 id="account">Вход выполнен</h1>',
            )
        else:
            self.reply(404, "Missing")

    def do_POST(self):
        raw = self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 200000))
        values = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        path = urlsplit(self.path).path
        if path == "/api/data":
            try:
                body = json.loads(raw)
            except ValueError:
                self.reply(400, "Invalid JSON")
                return
            if not isinstance(body, dict) or not isinstance(body.get("flag"), bool):
                self.reply(400, "Expected boolean flag")
                return
            self.server.api_posts += 1
            self.reply(200, "OK")
        elif path == "/save":
            identity = str(len(self.server.posts) + 1)
            self.server.posts[identity] = {
                "note": values.get("note", [""])[0],
                "mode": values.get("mode", [""])[0],
            }
            self.reply(303, headers={"Location": "/result/" + identity})
        elif path == "/login":
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            sid = cookies["sid"].value if "sid" in cookies else ""
            checks = {
                "user_matches": values.get("user") == ["demo"],
                "password_matches": values.get("password") == [DEMO_PASSWORD],
                "csrf_matches": values.get("csrf") == [self.server.tokens.get(sid)],
                "session_known": sid in self.server.tokens,
            }
            self.server.login_attempts.append(checks)
            valid = all(checks.values())
            if not valid:
                self.reply(403, "Rejected")
                return
            self.server.authorized.add(sid)
            self.server.login_count += 1
            self.reply(303, headers={"Location": "/account"})
        else:
            self.reply(404, "Missing")


@contextmanager
def serve_maker(port=0):
    server = FixtureServer(("127.0.0.1", port), MakerHandler)
    server.authorized = set()
    server.login_count = 0
    server.login_attempts = []
    server.api_posts = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
