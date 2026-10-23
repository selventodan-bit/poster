"""Test real npm editor distributions: fill through Poster, submit, inspect server data.

Install with npm ci --prefix scripts/integration/editors first. Downloads and
bundles live outside the repository's source tree (.poster by default).
"""

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from bs4 import BeautifulSoup

from poster.recognition.editors import EDITORS
from poster.recognition.forms import UniversalFormDetector
from poster.recorder.capture import chromium_options

VERSIONS = {
    "tinymce": "7.9.1",
    "ckeditor": "4.22.1",
    "quill": "2.0.3",
    "prosemirror": "Tiptap 2.27.2",
    "summernote": "0.9.1",
}


def document(editor):
    scripts = {
        "tinymce": '<script src="/node_modules/tinymce/tinymce.min.js"></script>',
        "ckeditor": '<script src="/node_modules/ckeditor4/ckeditor.js"></script>',
        "quill": '<script src="/node_modules/quill/dist/quill.js"></script>',
        "prosemirror": '<script src="/tiptap.bundle.js"></script>',
        "summernote": '<script src="/node_modules/jquery/dist/jquery.min.js"></script>'
        '<link rel="stylesheet" href="/node_modules/summernote/dist/summernote-lite.min.css">'
        '<script src="/node_modules/summernote/dist/summernote-lite.min.js"></script>',
    }
    setup = {
        "tinymce": "tinymce.init({selector:'#editor',license_key:'gpl',promotion:false,menubar:false,"
        "setup:e=>e.on('init',()=>window.ready=true)});",
        "ckeditor": "CKEDITOR.replace('editor',{versionCheck:false}).on('instanceReady',()=>window.ready=true);",
        "quill": "window.editor=new Quill('#editor',{theme:null});window.ready=true;",
        "prosemirror": "window.editor=PosterTiptap();window.ready=true;",
        "summernote": "$('#editor').summernote();window.ready=true;",
    }
    body = (
        '<div id="editor"></div><input type="hidden" name="body">'
        if editor in {"quill", "prosemirror"}
        else '<textarea id="editor" name="body"></textarea>'
    )
    submit = {
        "tinymce": "tinymce.triggerSave();",
        "ckeditor": "CKEDITOR.instances.editor.updateElement();",
        "quill": "document.querySelector('[name=body]').value=editor.root.innerHTML;",
        "prosemirror": "document.querySelector('[name=body]').value=editor.getHTML();",
        "summernote": "document.querySelector('[name=body]').value=$('#editor').summernote('code');",
    }
    return (
        f'<!doctype html><meta charset="utf-8">{scripts[editor]}'
        f'<form method="post" action="/submit"><input name="title" aria-label="Title">{body}'
        f'<button type="submit">Publish</button></form><script>{setup[editor]}'
        f"document.querySelector('form').onsubmit=()=>{{{submit[editor]}}};</script>"
    )


async def check(directory, output):
    from playwright.async_api import async_playwright

    directory = directory.resolve()
    output.mkdir(parents=True, exist_ok=True)
    entry = directory / "poster-tiptap.js"
    source = Path("scripts/integration/editors/tiptap.js").resolve()
    shutil.copyfile(source, entry)
    subprocess.run(
        [
            str(directory / "node_modules/.bin/esbuild"),
            str(entry),
            "--bundle",
            "--format=iife",
            "--outfile=" + str(directory / "tiptap.bundle.js"),
        ],
        check=True,
    )
    received = []

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/editor/"):
                data = document(self.path.rsplit("/", 1)[-1]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(data)
            else:
                super().do_GET()

        def do_POST(self):
            received.append(parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Saved by controlled test server")

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(directory)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    report = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(**chromium_options(True, p.chromium.executable_path))
            for name, version in VERSIONS.items():
                page = await browser.new_page()
                await page.goto(f"http://127.0.0.1:{server.server_port}/editor/{name}")
                await page.wait_for_function("window.ready===true", timeout=20000)
                forms = await UniversalFormDetector().inspect(page, ["127.0.0.1"])
                if not any({"title", "body", "submit"} <= f.fields.keys() for f in forms):
                    raise AssertionError(
                        (name, [(f.key, sorted(f.fields)) for f in forms], [f.url for f in page.frames])
                    )
                form = next(f for f in forms if {"title", "body", "submit"} <= f.fields.keys())
                ref = form.fields["body"]
                value = 'Unicode Ж العربية 日本語 & <tag> "quote"\nSecond line\n\nThird paragraph'
                await EDITORS[name].fill(page.frames[ref.frame].locator(ref.selector), value)
                await page.locator('input[name="title"]').fill("Original " + name)
                before = len(received)
                await page.locator('button[type="submit"]').click()
                assert len(received) == before + 1
                actual = received[-1]["body"][0]
                text = BeautifulSoup(actual, "html.parser").get_text(" ", strip=True)
                assert " ".join(text.split()) == " ".join(value.split()), (name, actual)
                assert "&lt;tag&gt;" in actual, (name, "literal markup was interpreted")
                tree = BeautifulSoup(actual, "html.parser")
                blocks = tree.select("p") or [tree]
                lines = []
                for block in blocks:
                    if len(block.contents) == 1 and getattr(block.contents[0], "name", None) == "br":
                        block.clear()
                    for br in block.select("br"):
                        # CKEditor's HTML writer adds formatting CRLF after a
                        # <br>. That whitespace is collapsed by HTML rendering;
                        # it is not an additional user-entered line break.
                        following = br.next_sibling
                        if isinstance(following, str):
                            following.replace_with(re.sub(r"^\r?\n[\t ]*", "", following, count=1))
                        br.replace_with("\n")
                    lines.append(block.get_text())
                assert "\n".join(lines).rstrip("\n") == value, (name, "line breaks", actual)
                report.append(
                    {
                        "editor": name,
                        "version": version,
                        "server_text_matches": True,
                        "literal_markup_preserved": True,
                        "line_breaks_preserved": True,
                        "submissions": 1,
                    }
                )
                await page.close()
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    (output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=Path("scripts/integration/editors"))
    parser.add_argument("--output", type=Path, default=Path(".poster/original-editors"))
    args = parser.parse_args()
    asyncio.run(check(args.directory, args.output))
