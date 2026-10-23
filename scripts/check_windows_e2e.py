"""Real Chromium -> independent HTTP -> new process Runtime on any OS.

Windows CI runs this from an unpacked release ZIP in a path with spaces/Cyrillic.
GUI initialization/clipboard are tested separately; this is a service E2E.
"""

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import traceback

from poster.auth import ensure_local_key
from poster.fixtures_readiness import serve_readiness
from poster.recognition.knowledge import KnowledgeBase
from poster.recognition.service import RecognitionSession


async def check(directory, case="topic"):
    directory.mkdir(parents=True, exist_ok=True)
    ensure_local_key(directory / "private")
    with serve_readiness() as (server, base):
        kb = KnowledgeBase(directory / "knowledge.sqlite")
        session = RecognitionSession(
            base + "/compose/" + case, directory / "profile", knowledge=kb, headless=True
        )
        try:
            await session.start()
            result = await session.recognize()
            assert result.state == "DETECTED", result.summary()
            title = "Windows Ж 日本語" if case not in {"reply", "comment"} else ""
            await session.test_publish(title, "Браузер & <literal>\nسطر 日本語", "acceptance", confirmed=True)
            assert session.result.state == "HTTP_CANDIDATE", session.result.summary()
            # The HTTP verifier must not use a living browser.
            await session.context.close()
            session.context = None
            await session.playwright.stop()
            session.playwright = None
            result = await session.verify_http(
                title + " HTTP" if title else "", "HTTP & <literal>\nЖ سطر", confirmed=True
            )
            assert result.status == "SUCCEEDED", result
            assert session.result.state == "HTTP_VERIFIED"
            project = directory / "saved project Кириллица"
            session.save(project)
            assert kb.rules()[0].http_successes == 1
        finally:
            await session.close()
            kb.close()
        code = """import asyncio,json,sys
from pathlib import Path
from poster.project import load_project
from poster.runtime import Runtime
from poster.recognition.knowledge import KnowledgeBase
assert not any(k.startswith('playwright') for k in sys.modules)
p=load_project(Path(sys.argv[1]))
r=asyncio.run(Runtime(p).run({'title':'Restart Ж','body':'Runtime & <literal>\\n日本語 سطر'}))
assert r.status=='SUCCEEDED',r
k=KnowledgeBase(Path(sys.argv[2]));assert k.rules()[0].http_successes==1;k.close()
print(json.dumps({'status':r.status,'browser_imported':False}))
"""
        process = subprocess.run(
            [sys.executable, "-c", code, str(project), str(directory / "knowledge.sqlite")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert process.returncode == 0, process.stderr
        assert (
            len(server.posts) == 3
            and server.writes
            == (["/store-js"] if case in {"ajax", "background", "synchronous", "listener"} else ["/store"])
            * 3
        ), server.writes
        assert all("<literal>" in p["body"] and "\n" in p["body"] for p in server.posts.values())
        report = {
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "case": case,
            "browser": True,
            "http": True,
            "runtime_new_process": True,
            "knowledge_restart": True,
            "writes": 3,
            "gui_e2e": False,
        }
        (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(".poster/windows-e2e"))
    parser.add_argument("--case", default="topic")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(check(args.output, args.case))))
    except Exception as exc:
        locations = " > ".join(
            f"{Path(f.filename).name}:{f.lineno}:{f.name}"
            for f in traceback.extract_tb(exc.__traceback__)[-4:]
        )
        print(f"::error::Windows acceptance {type(exc).__name__} at {locations}", flush=True)
        raise
