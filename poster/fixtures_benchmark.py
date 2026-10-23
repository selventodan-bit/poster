"""Independent controlled corpus renderer; it is not a CMS emulator or a production adapter."""

import hashlib
import html
import json
import secrets
import re
import threading
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

from .fixtures import FixtureServer, Handler
from .recognition.network import decode, text

LABELS = {
    "en": ("Subject", "Message", "Publish", "New Thread"),
    "es": ("Asunto", "Mensaje", "Enviar", "Nuevo debate"),
    "fr": ("Titre", "Texte", "Envoyer", "Nouveau sujet"),
    "de": ("Betreff", "Nachricht", "Veröffentlichen", "Neues Thema"),
    "it": ("Titolo", "Messaggio", "Invia", "Nuovo argomento"),
    "pt": ("Título", "Mensagem", "Publicar", "Criar tópico"),
    "ru": ("Заголовок", "Сообщение", "Отправить", "Новая тема"),
    "uk": ("Тема", "Повідомлення", "Надіслати", "Створити тему"),
    "pl": ("Tytuł", "Treść", "Opublikuj", "Nowy temat"),
    "ar": ("عنوان", "محتوى", "نشر", "موضوع جديد"),
    "he": ("כותרת", "תוכן", "פרסם", "דיון חדש"),
}


def page(case, token):
    if case.get("decoy"):
        return {
            "login": '<form action="/login"><input name="username"><input type="password"><button>Login</button></form>',
            "search": '<form role="search"><input type="search" aria-label="Search"><button>Search</button></form>',
            "profile": '<form action="/profile"><input aria-label="Name"><textarea aria-label="Biography"></textarea><button>Save</button></form>',
            "contact": '<form aria-label="Contact"><input aria-label="Your name"><textarea aria-label="Message"></textarea><button>Submit</button></form>',
            "reply": '<form><textarea aria-label="Message"></textarea><button>Submit</button></form>',
        }[case["decoy"]]
    a, b, publish, compose = LABELS[case["locale"]]
    editor = case["editor"]
    variant = case.get("layout", "classic")
    fingerprint = hashlib.sha256(case["id"].encode()).hexdigest()[:7]
    title = f'<label>{a}<input id="heading" name="x_{fingerprint}" maxlength="180" required></label>'
    if case.get("unknown"):
        title = '<input id="heading" name="opaque_a" required>'
    if editor == "textarea":
        body = f'<textarea id="writing" name="y_{fingerprint}" aria-label="{b}"></textarea>'
    elif editor == "iframe":
        body = '<iframe id="writing_ifr" src="/bench-editor"></iframe>'
    else:
        css = {
            "tinymce": "mce-content-body",
            "ckeditor": "ck-editor__editable",
            "quill": "ql-editor",
            "prosemirror": "ProseMirror",
            "summernote": "note-editable",
            "froala": "fr-element",
        }.get(editor, "writing")
        body = (
            f'<div id="writing" class="{css}" contenteditable="true" role="textbox" aria-label="{b}"></div>'
        )
    if editor == "quill":
        body = '<div class="ql-container">' + body + "</div>"
    extra = '<input name="aux" aria-label="Reference">' if case.get("unknown") else ""
    controls = body + title + extra if case.get("reorder") else title + body + extra
    if case.get("aux_first"):
        controls = extra + body + title
    if case.get("staged"):
        controls = (
            '<section id="step-one">'
            + title
            + '<button type="button" aria-controls="step-two" onclick="this.parentElement.hidden=true;document.querySelector(\'#step-two\').hidden=false">Next</button></section><section id="step-two" hidden>'
            + body
            + "</section>"
        )
    root = "section" if variant == "spa" else "form"
    form = f'<{root} id="composer" role="form" method="post" action="/bench-api/{case["id"]}">{controls}<input type="hidden" name="csrf_token" value="{token}"><button type="submit">{publish}</button></{root}>'
    nav = case.get("nav", "direct")
    target = case.get("opened", False)
    outer = form
    setup = "const scope=document;"
    if variant == "shadow":
        outer = '<x-forum id="island"></x-forum>'
        setup = (
            'const scope=document.querySelector("#island").attachShadow({mode:"open"});scope.innerHTML='
            + json.dumps(form)
            + ";"
        )
    if nav == "link" and not target:
        outer = f'<nav><a href="/bench/{case["id"]}?compose=1">{compose}</a></nav>'
        setup = "const scope=document;"
    elif nav == "menu" and not target:
        outer = f'<header><button type="button" aria-haspopup="menu" aria-controls="items" aria-expanded="false">☰</button><nav id="items" role="menu" hidden><a role="menuitem" href="/bench/{case["id"]}?compose=1">{compose}</a></nav></header>'
        setup = "const scope=document;document.querySelector('button').onclick=e=>{document.querySelector('#items').hidden=false;e.currentTarget.setAttribute('aria-expanded','true')};"
    elif nav in {"icon", "modal", "tab"} and not target:
        outer = f'<nav><button type="button" aria-controls="panel" {"role=tab" if nav == "tab" else ""}>{"✎" if nav == "icon" else compose}</button></nav><section id="panel" hidden>{form}</section>'
        setup = "const scope=document;document.querySelector('nav button').onclick=()=>document.querySelector('#panel').hidden=false;"
    listener = r"""const f=scope.querySelector('#composer');
    if(f) {
      const title=()=>f.querySelector('#heading').value;
      const e=f.querySelector('#writing'), iframe=f.querySelector('iframe');
      let editorState='';
      if(EDITOR==='quill') {e.parentElement.__quill={setText:v=>{editorState=v;e.textContent=v}};}
      if(EDITOR==='ckeditor') {e.ckeditorInstance={setData:v=>{editorState=v;e.innerHTML=v}};}
      if(EDITOR==='tinymce') {window.tinymce={get:()=>({setContent:v=>{editorState=v;e.innerHTML=v},save:()=>{}})};}
      const body=()=>iframe?iframe.contentDocument.body.innerHTML:
       ['quill','ckeditor','tinymce'].includes(EDITOR)?editorState:e.isContentEditable?e.innerHTML:e.value;
      const send=async()=>{
        if(NEG==='background') {
          await fetch('/bench-api/side-effect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({setting:true})}).catch(()=>{});
        }
        let data={subject:title(),message:body()},endpoint='/bench-api/'+ID,options={method:'POST'};
        if(PROTOCOL==='draft') {
          const draft=await(await fetch('/bench-api/draft',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({subject:title()})})).json();
          await fetch('/bench-api/'+draft.draft.id+'/body',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:body(),draft_id:draft.draft.id})});
          endpoint='/bench-api/'+draft.draft.id+'/publish';data={draft_id:draft.draft.id,scenario:ID};
        }
        if(PROTOCOL==='header') {
          const fresh=await fetch('/bench-token');options.headers={'Content-Type':'application/json','X-CSRF-Token':fresh.headers.get('X-CSRF-Token')};
        }
        if(PROTOCOL==='multipart') {const fd=new FormData();fd.set('subject',title());fd.set('message',body());options.body=fd;}
        else if(PROTOCOL==='form') {options.body=new URLSearchParams({...data,csrf_token:f.querySelector('[name=csrf_token]').value});}
        else {options.headers={...options.headers,'Content-Type':'application/json'}; options.body=JSON.stringify(PROTOCOL==='array'?{topics:[data]}:data);}
        if(NEG==='opaque') options.body=JSON.stringify({...data,nonce:crypto.randomUUID()});
        const result=await(await fetch(endpoint,options)).json();
        if(result.url) {const a=document.createElement('a');a.rel='bookmark';a.href=result.url;a.textContent='View';document.body.append(a);}
      };
      if(f.tagName==='FORM') f.addEventListener('submit',e=>{e.preventDefault();send()});
      else f.querySelector('button').addEventListener('click',send);
    }"""
    for key, value in {
        "EDITOR": editor,
        "PROTOCOL": case["protocol"],
        "ID": case["id"],
        "NEG": case.get("negative", ""),
    }.items():
        listener = re.sub(r"\b" + key + r"\b", lambda _, value=value: json.dumps(value), listener)
    return (
        '<!doctype html><meta charset="utf-8"><html dir="'
        + ("rtl" if case["locale"] in {"ar", "he"} else "ltr")
        + '"><body>'
        + outer
        + "<script>"
        + setup
        + listener
        + "</script></body></html>"
    )


class BenchmarkHandler(Handler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/bench-editor":
            self.reply(
                200, '<!doctype html><body contenteditable="true" role="textbox" aria-label="Message"></body>'
            )
        elif parsed.path == "/bench-token":
            token = secrets.token_hex(18)
            self.server.bench_tokens.add(token)
            self.reply(200, "{}", {"Content-Type": "application/json", "X-CSRF-Token": token})
        elif parsed.path.startswith("/bench/"):
            key = parsed.path.rsplit("/", 1)[-1]
            case = {**self.server.cases[key], "opened": bool(parse_qs(parsed.query).get("compose"))}
            token = secrets.token_hex(18)
            self.server.bench_tokens.add(token)
            self.reply(200, page(case, token))
        elif parsed.path.startswith("/bench-result/"):
            key = parsed.path.rsplit("/", 1)[-1]
            data = self.server.posts[key]
            title, body = html.escape(data["title"]), html.escape(data["body"])
            if data.get("negative") == "login":
                self.reply(
                    200,
                    f'<form><input type="password"></form><article><h1>{title}</h1><p>{body}</p></article>',
                )
            elif data.get("negative") == "hidden":
                self.reply(200, f"<section hidden><h1>{title}</h1><p>{body}</p></section>")
            else:
                self.reply(
                    200,
                    f'<!doctype html><meta charset="utf-8"><main><article><header><h1>{title}</h1></header><section class="prose"><p>{body}</p></section></article></main>',
                )
        else:
            super().do_GET()

    def do_PATCH(self):
        self.do_POST()

    def do_POST(self):
        path = urlsplit(self.path).path
        if not path.startswith("/bench-api/"):
            super().do_POST()
            return
        key = path.rsplit("/", 1)[-1]
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        _, data = decode({"content_type": self.headers.get("content-type", ""), "body": raw})
        data = data.get("topics", [data])[0]
        self.server.writes.append(path)
        if key == "side-effect":
            self.reply(200, "{}")
            return
        if key == "draft":
            identifier = secrets.token_hex(14)
            self.server.drafts[identifier] = {"title": data["subject"]}
            self.reply(200, json.dumps({"draft": {"id": identifier}}))
            return
        if key == "body":
            identifier = path.split("/")[-2]
            self.server.drafts[identifier]["body"] = text(data["message"])
            self.reply(200, json.dumps({"id": identifier}))
            return
        if key == "publish":
            identifier = path.split("/")[-2]
            if identifier != data["draft_id"]:
                self.reply(422, "{}")
                return
            record = self.server.drafts[identifier]
            key = data["scenario"]
            data = {"subject": record["title"], "message": record["body"]}
        case = self.server.cases[key]
        if case["protocol"] in {"form", "header"}:
            token = data.get("csrf_token") if case["protocol"] == "form" else self.headers.get("X-CSRF-Token")
            if token not in self.server.bench_tokens:
                self.reply(403, "{}")
                return
            self.server.bench_tokens.remove(token)
        if case.get("negative") == "rejected":
            self.reply(200, '{"success":false}')
            return
        if case.get("negative") == "slow":
            time.sleep(float(case.get("response_delay", 0.25)))
        identifier = secrets.token_hex(8)
        self.server.posts[identifier] = {
            "title": data["subject"],
            "body": text(data["message"]),
            "negative": case.get("negative"),
        }
        self.reply(
            200,
            json.dumps(
                {"accepted": True}
                if case.get("negative") == "silent"
                else {"url": "/bench-result/" + identifier}
            ),
        )


@contextmanager
def serve_benchmark(cases):
    server = FixtureServer(("127.0.0.1", 0), BenchmarkHandler)
    server.cases = {c["id"]: c for c in cases}
    server.bench_tokens = set()
    server.drafts = {}
    server.writes = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
