"""Real JS/AJAX fixtures used by the UI and browser/HTTP acceptance tests."""

import json
import secrets
import threading
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from .fixtures import FixtureServer, Handler

KINDS = (
    "classic",
    "ajax",
    "json",
    "multipart",
    "contenteditable",
    "iframe",
    "csrf",
    "dynamic",
    "multiple",
    "split",
    "silent",
    "failed",
    "unknown-token",
    "theme-b",
    "ambiguous",
    "auth",
    "no-form",
    "split-numeric",
)


class RecognitionServer(FixtureServer):
    def __init__(self, address):
        super().__init__(address, RecognitionHandler)
        self.drafts, self.write_counts = {}, {}


class RecognitionHandler(Handler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/v2/editor":
            self.reply(200, '<!doctype html><body contenteditable="true" aria-label="Mensaje"></body>')
            return
        if not path.startswith("/recognize/"):
            super().do_GET()
            return
        kind = path.rsplit("/", 1)[-1]
        if kind not in KINDS:
            self.reply(404)
            return
        headers, token_html = {}, ""
        if kind == "csrf":
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            sid = cookies["sid"].value if "sid" in cookies else secrets.token_hex(16)
            token = secrets.token_hex(24)
            with self.server.lock:
                self.server.tokens[sid] = token
                self.server.token_history.append(token)
            headers["Set-Cookie"] = f"sid={sid}; HttpOnly; Path=/; SameSite=Lax"
            token_html = f'<input type="hidden" name="csrf_token" value="{token}">'
        if kind == "unknown-token":
            token_html = "<script>window.secretNonce=crypto.randomUUID()</script>"
        editor = '<textarea aria-label="Mensaje" name="message"></textarea>'
        if kind in {"contenteditable", "theme-b"}:
            editor = '<div contenteditable="true" role="textbox" aria-label="Mensaje"></div>'
        if kind == "iframe":
            editor = '<iframe title="Editor" src="/v2/editor"></iframe>'
        field_name = "different_subject" if kind == "theme-b" else "subject"
        form = f'''<form method="post" action="/v2/{kind}" id="compose">{token_html}
          <label>Asunto <input name="{field_name}"></label>{editor}
          <button type="submit">Enviar</button></form>'''
        if kind == "theme-b":
            form = (
                "<style>form{background:#eef;padding:2em;display:grid;gap:1em}</style>"
                '<form id="compose" role="form"><label>Reference <input name="alpha"></label>'
                '<label>Extra <input name="beta"></label>'
                + editor
                + '<button type="submit">Enviar</button></form>'
            )
        if kind == "no-form":
            form = (
                form.replace("<form ", "<section ")
                .replace("</form>", "</section>")
                .replace('type="submit"', 'type="button"')
            )
        if kind == "auth":
            form = '<form><input type="password" aria-label="Password"><button>Login</button></form>'
        if kind == "ambiguous":
            form += form.replace('id="compose"', 'id="second"')
        extra = (
            '<form><input type="search" aria-label="Buscar"><button>Buscar</button></form>'
            if kind == "multiple"
            else ""
        )
        listener = r"""const f=document.querySelector('#compose');
         f.addEventListener('submit', async e=>{
          e.preventDefault();
          const title=f.querySelector('input:not([type=hidden])').value;
          const rich=f.querySelector('[contenteditable]');
          const editor=f.querySelector('iframe');
          const body=rich ? rich.innerHTML : editor ? editor.contentDocument.body.innerHTML : f.querySelector('textarea').value;
          let options={method:'POST'}, endpoint='/v2/KIND';
          if(KIND==='ajax' || KIND==='csrf' || KIND==='multiple' || KIND==='dynamic') {
           options.body=new URLSearchParams(new FormData(f));
          } else if(KIND==='multipart') { options.body=new FormData(f); }
          else {
           let payload={subject:title,message:body};
           if(KIND==='split' || KIND==='split-numeric') {
            const draft=await (await fetch(KIND==='split'?'/v2/drafts':'/v2/drafts-numeric',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({subject:title})})).json();
            payload={draft_id:draft.draft.id,message:body};
           }
           if(KIND==='unknown-token') payload.csrf_nonce=window.secretNonce;
           options.headers={'Content-Type':'application/json'};
           options.body=JSON.stringify(KIND==='json'?{topic:payload}:payload);
          }
          const response=await fetch(endpoint,options); const result=await response.json();
          if(result.url) { const a=document.createElement('a'); a.rel='bookmark';a.href=result.url;a.textContent='View publication';document.body.append(a); }
         });""".replace("'/v2/KIND'", repr("/v2/" + kind)).replace("KIND", json.dumps(kind))
        if kind == "classic":
            script = ""
        elif kind == "dynamic":
            script = (
                "document.querySelector('#open').onclick=()=>{document.querySelector('#container').innerHTML="
                + json.dumps(form)
                + ";"
                + listener
                + "}"
            )
            form = '<button id="open" type="button">New topic</button><section id="container"></section>'
        elif kind == "auth":
            script = ""
        else:
            if kind == "no-form":
                listener = listener.replace(
                    "f.addEventListener('submit',", "f.querySelector('button').addEventListener('click',"
                )
            script = listener
        self.reply(
            200,
            '<!doctype html><meta charset="utf-8"><title>Compose</title>'
            + extra
            + form
            + "<script>"
            + script
            + "</script>",
            headers,
        )

    def do_POST(self):
        path = urlsplit(self.path).path
        if not path.startswith("/v2/"):
            super().do_POST()
            return
        kind = path.rsplit("/", 1)[-1]
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            self.reply(413)
            return
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        with self.server.lock:
            self.server.write_counts[kind] = self.server.write_counts.get(kind, 0) + 1
        if "application/json" in content_type:
            data = json.loads(raw)
            data = data.get("topic", data)
        elif "multipart/form-data" in content_type:
            message = BytesParser(policy=policy.default).parsebytes(
                ("Content-Type: " + content_type + "\r\n\r\n").encode() + raw
            )
            data = {
                part.get_param("name", header="content-disposition"): part.get_payload(decode=True).decode()
                for part in message.iter_parts()
            }
        else:
            data = {k: v[0] for k, v in parse_qs(raw.decode(), keep_blank_values=True).items()}
        if kind in {"drafts", "drafts-numeric"}:
            identifier = len(self.server.drafts) + 1 if kind == "drafts-numeric" else secrets.token_hex(12)
            self.server.drafts[identifier] = data["subject"]
            self.reply(200, json.dumps({"draft": {"id": identifier}}))
            return
        if kind == "csrf":
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            sid = cookies["sid"].value if "sid" in cookies else ""
            with self.server.lock:
                token = self.server.tokens.pop(sid, None)
            if not token or data.get("csrf_token") != token:
                self.reply(403, json.dumps({"success": False}))
                return
        if kind == "failed":
            self.reply(200, json.dumps({"success": False, "error": "validation"}))
            return
        title = (
            self.server.drafts.get(data.get("draft_id"))
            if kind in {"split", "split-numeric"}
            else data.get("subject", data.get("different_subject"))
        )
        body = data.get("message", "")
        if kind in {"contenteditable", "iframe", "theme-b"}:
            body = BeautifulSoup(body, "html.parser").get_text(strip=True)
        if not title or not body:
            self.reply(422, json.dumps({"success": False}))
            return
        post_id = secrets.token_hex(8)
        with self.server.lock:
            self.server.posts[post_id] = {"title": title, "body": body, "site": kind}
        if kind == "silent":
            self.reply(200, json.dumps({"accepted": True}))
        elif kind == "classic":
            self.reply(303, headers={"Location": "/posts/" + post_id})
        else:
            self.reply(200, json.dumps({"url": "/posts/" + post_id}))


@contextmanager
def serve_recognition_fixture(host="127.0.0.1", port=0):
    server = RecognitionServer((host, port))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://{host}:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def serve_forever(port=8765):
    print(f"Recorder 2.0 fixtures on 127.0.0.1:{port}: /recognize/ajax (plus {', '.join(KINDS)})", flush=True)
    RecognitionServer(("127.0.0.1", port)).serve_forever()
