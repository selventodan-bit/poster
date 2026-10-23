"""Independent localhost acceptance pages; no CMS compatibility claims."""

import html
import json
import secrets
from contextlib import contextmanager
import threading
from urllib.parse import parse_qs, urlsplit

from .fixtures import FixtureServer, Handler


class ReadinessHandler(Handler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/compose/"):
            case = path.rsplit("/", 1)[-1]
            kind = case if case in {"reply", "comment"} else "topic"
            title = '<input name="x47" required>' if kind == "topic" else ""
            js = case in {"ajax", "background", "ambiguous", "delayed", "synchronous"}
            token = secrets.token_hex(24)
            self.server.tokens[token] = True
            # The opaque names/classes and unknown language are independent of the old generator.
            button = "<button>ހުށަހެޅުން</button>"
            if js:
                button = '<button type="button" aria-controls="z91" onclick="emit()"><svg width="20" height="20"><circle cx="8" cy="8" r="6"/></svg></button>'
            if case == "ambiguous":
                button += button
            markup = f'''<form class="editor" data-publication-type="{kind}" method="post" action="/store">
              <input type="hidden" name="csrf_token" value="{token}">{title}
              <div><textarea id="z91" name="q86" required></textarea></div>{button}</form>'''
            if case == "unsafe":
                markup = markup.replace("/store", "/edit")
            script = r"""
            const f=document.querySelector('form');
            const payload=()=>new URLSearchParams(new FormData(f));
            function emit(){fetch('/store-js',{method:'POST',body:payload()}).then(r=>r.json()).then(r=>{location.href=r.url;});}
            """
            if case == "synchronous":
                script = script.replace(
                    "function emit(){",
                    "function emit(){fetch('/autosave',{method:'POST',body:payload()}).catch(()=>{});",
                )
            if case == "background":
                script += r"""
                document.querySelector('button').addEventListener('pointerdown',()=>{
                  setTimeout(()=>fetch('/store-js',{method:'POST',body:payload()}).catch(()=>{}),0);
                });
                setInterval(()=>{if(f.q86.value) fetch('/autosave',{method:'POST',body:payload()}).catch(()=>{});},30);
                """
            if case == "delayed":
                script = script.replace("fetch('/store'", "setTimeout(()=>fetch('/store'").replace(
                    "location.href=r.url;});}", "location.href=r.url;}),50);}"
                )
            if not js:
                script = ""
            self.reply(
                200, '<!doctype html><meta charset="utf-8">' + markup + "<script>" + script + "</script>"
            )
        elif path.startswith("/entry/"):
            item = self.server.posts.get(path.rsplit("/", 1)[-1])
            if not item:
                self.reply(404)
            else:
                self.reply(
                    200,
                    "<article><h1>"
                    + html.escape(item["title"])
                    + "</h1><pre>"
                    + html.escape(item["body"])
                    + "</pre></article>",
                )
        else:
            super().do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        values = parse_qs(
            self.rfile.read(int(self.headers.get("Content-Length", 0))).decode(), keep_blank_values=True
        )
        self.server.writes.append(path)
        assert not self.headers.get("x-poster-interaction"), "Private attribution header leaked"
        token = values.get("csrf_token", [""])[0]
        if path not in {"/store", "/store-js"} or token not in self.server.tokens:
            self.reply(403)
            return
        del self.server.tokens[token]
        identity = secrets.token_hex(8)
        self.server.posts[identity] = {"title": values.get("x47", [""])[0], "body": values["q86"][0]}
        location = "/entry/" + identity
        if path == "/store":
            self.reply(303, headers={"Location": location})
        else:
            self.reply(200, json.dumps({"url": location}))


@contextmanager
def serve_readiness():
    server = FixtureServer(("127.0.0.1", 0), ReadinessHandler)
    server.writes = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
