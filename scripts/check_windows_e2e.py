"""Real Chromium -> independent HTTP -> new process Runtime on any OS.

Windows CI runs this from an unpacked release ZIP in a path with spaces/Cyrillic.
GUI initialization/clipboard are tested separately; this is a service E2E.
"""

import argparse
import asyncio
import json
import os
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
            if session.result.state != "HTTP_CANDIDATE":
                diagnostic_path = session.profile.parent / "diagnostics.jsonl"
                if diagnostic_path.is_file():
                    try:
                        rows = [
                            json.loads(line)
                            for line in diagnostic_path.read_text(encoding="utf-8").splitlines()[-5:]
                        ]
                        safe = [
                            {
                                k: row.get(k)
                                for k in ("code", "exception", "phase_hint", "read_only", "write_admitted")
                            }
                            for row in rows
                        ]
                        print(
                            "::error title=Windows acceptance safe diagnostics::" + json.dumps(safe),
                            flush=True,
                        )
                    except (OSError, ValueError):
                        pass
                print(
                    "::error title=Windows acceptance state::"
                    + json.dumps(
                        {
                            "state": str(session.result.state),
                            "outcome": session.result.outcome,
                            "diagnostic_codes": session.result.diagnostics,
                            "write_admitted": session.write_admitted,
                            "server_writes": len(server.writes),
                            "captured_requests": len(session.recorder.capture.requests),
                            "captured_responses": len(session.recorder.capture.responses),
                        }
                    ),
                    flush=True,
                )
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
            assert server.authorized_sid not in (project / "workflow.yaml").read_text(encoding="utf-8")
            assert server.authorized_sid not in kb.rules()[0].model_dump_json()
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
            == (
                ["/store-js"]
                if case in {"ajax", "background", "synchronous", "listener", "toolbar"}
                else ["/store"]
            )
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
            "encrypted_authorization_reused": True,
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
        report_json = json.dumps(asyncio.run(check(args.output, args.case)))
        print(
            ("::notice title=Windows E2E report::" if os.environ.get("GITHUB_ACTIONS") else "") + report_json
        )
    except Exception as exc:
        locations = " > ".join(
            f"{Path(f.filename).name}:{f.lineno}:{f.name}"
            for f in traceback.extract_tb(exc.__traceback__)[-4:]
        )
        print(f"::error::Windows acceptance {type(exc).__name__} at {locations}", flush=True)
        raise
