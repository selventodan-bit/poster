"""Finite real-browser demo of automatic recognition, learning, and independent HTTP replay."""

import json
from pathlib import Path

from .auth import ensure_local_key
from .fixtures_recognition import serve_recognition_fixture
from .recognition.knowledge import KnowledgeBase
from .recognition.service import RecognitionSession


async def demo(
    output: Path, kinds=("ajax", "json", "multipart", "contenteditable", "iframe", "csrf", "split")
):
    if output.exists() and any(output.iterdir()):
        raise ValueError("Demo output must be empty")
    output.mkdir(parents=True, exist_ok=True)
    ensure_local_key(output / "private")
    knowledge = KnowledgeBase(output / "knowledge.sqlite")
    report = {"status": "SUCCEEDED", "cases": [], "browser_writes": 0, "verified_http_writes": 0}
    try:
        with serve_recognition_fixture() as (server, base):
            for kind in kinds:
                session = RecognitionSession(
                    base + "/recognize/" + kind,
                    output / "profiles" / kind,
                    knowledge=knowledge,
                    headless=True,
                )
                try:
                    recognition = await session.start()
                    await recognition.recognize()
                    assert not session.browser_attempted
                    before = len(server.posts)
                    await session.test_publish(
                        "Browser " + kind, "Browser body & тест.", "v2-" + kind, confirmed=True
                    )
                    assert session.result.state == "HTTP_CANDIDATE", session.result.summary()
                    assert len(server.posts) == before + 1
                    await session.close()
                    result = await session.verify_http(
                        "HTTP " + kind, "HTTP body & no browser.", confirmed=True
                    )
                    assert result.status == "SUCCEEDED", result
                    assert len(server.posts) == before + 2
                    session.save(output / "projects" / ("v2-" + kind))
                    report["cases"].append(
                        {
                            "fixture": kind,
                            "state": str(session.result.state),
                            "history": session.history,
                            "confidence": session.result.confidence.__dict__,
                        }
                    )
                    report["browser_writes"] += 1
                    report["verified_http_writes"] += 1
                finally:
                    await session.close()
            report["independent_permalink_checks"] = True
            report["note"] = "Controlled fixture servers stopped after the demo"
            (output / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    finally:
        knowledge.close()
    return report
