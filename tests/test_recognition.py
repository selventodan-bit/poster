import json

import pytest

from poster.fixtures_recognition import serve_recognition_fixture
from poster.project import load_project
from poster.recognition.detector import EngineDetector
from poster.recognition.knowledge import KnowledgeBase
from poster.recognition.service import RecognitionSession
from poster.recognition.types import LearningState
from poster.runtime import Runtime


@pytest.fixture
def recognition_site():
    with serve_recognition_fixture() as result:
        yield result


@pytest.mark.parametrize(
    "kind",
    [
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
        "no-form",
        "split-numeric",
    ],
)
async def test_recognize_browser_learn_http_export(recognition_site, tmp_path, kind):
    server, base = recognition_site
    session = RecognitionSession(base + "/recognize/" + kind, tmp_path / "profile", headless=True)
    try:
        await session.start()
        result = await session.recognize()
        assert result.state == LearningState.DETECTED, result.summary()
        assert len(server.posts) == 0 and not server.write_counts
        with pytest.raises(PermissionError):
            await session.test_publish("Browser title", "Body & текст", kind)
        assert not server.posts
        result = await session.test_publish("Browser title", "Body & текст", kind, confirmed=True)
        assert result.state == LearningState.HTTP_CANDIDATE, result.summary()
        assert len(server.posts) == 1
        assert not result.confidence.http_verified
        candidate = tmp_path / "candidate"
        session.save(candidate)
        p = load_project(candidate)
        assert p.site.status == "REVIEW"
        p_force_ready = p.site.model_copy(update={"status": "HTTP_READY"})
        from poster.project import Project

        refused = await Runtime(Project(p_force_ready, p.workflow, p.revision, p.session)).run({})
        assert refused.status == "REVIEW_REQUIRED"
        with pytest.raises(PermissionError):
            await session.verify_http("HTTP title", "New & body")
        # Close the actual browser before proving that the learned project is independent.
        await session.close()
        verified = await session.verify_http("HTTP title", "New & body", confirmed=True)
        assert verified.status == "SUCCEEDED", (verified, session.build.workflow)
        assert session.result.state == LearningState.HTTP_VERIFIED
        assert len(server.posts) == 2
        with pytest.raises(ValueError, match="ALREADY_ATTEMPTED"):
            await session.verify_http("Duplicate", "Never", confirmed=True)
        session.save(tmp_path / "verified")
        p = load_project(tmp_path / "verified")
        assert p.site.status == "HTTP_READY" and p.site.recognition.http_verified
        if kind == "csrf":
            assert len(set(server.token_history)) >= 2
            exported = "\n".join(
                path.read_text()
                for path in (tmp_path / "verified").iterdir()
                if path.suffix in {".yaml", ".json"}
            )
            assert all(token not in exported for token in server.token_history)
            assert p.session and p.session.startswith(b"gAAAA")
        if kind == "split":
            assert server.write_counts["drafts"] == server.write_counts["split"] == 2
        if kind == "split-numeric":
            assert server.write_counts["drafts-numeric"] == server.write_counts[kind] == 2
    finally:
        await session.close()


@pytest.mark.parametrize("kind,state", [("silent", "UNKNOWN"), ("failed", "FAILED")])
async def test_no_confirmation_never_resends(recognition_site, tmp_path, kind, state):
    server, base = recognition_site
    session = RecognitionSession(
        base + "/recognize/" + kind, tmp_path / "profile", headless=True, confirmation_timeout=0.7
    )
    try:
        await session.start()
        await session.recognize()
        await session.test_publish("Title", "Body", kind, confirmed=True)
        assert session.result.state == state
        assert server.write_counts[kind] == 1
        with pytest.raises(ValueError, match="ALREADY_ATTEMPTED"):
            await session.test_publish("Title", "Body", kind, confirmed=True)
        with pytest.raises(ValueError):
            session.save(tmp_path / "unsafe")
        assert server.write_counts[kind] == 1
    finally:
        await session.close()


async def test_unknown_js_token_is_diagnosed_not_hardcoded(recognition_site, tmp_path):
    server, base = recognition_site
    session = RecognitionSession(base + "/recognize/unknown-token", tmp_path / "profile", headless=True)
    try:
        await session.start()
        await session.recognize()
        await session.test_publish("Title", "Body", "unknown", confirmed=True)
        assert session.result.state == "LEARNING_REQUIRED", session.result.summary()
        assert "UNKNOWN_DYNAMIC" in session.result.diagnostics
        assert len(server.posts) == 1  # Browser success does not establish HTTP readiness.
        assert "operator_review_required" in json.dumps(session.build.workflow)
    finally:
        await session.close()


async def test_auth_and_ambiguous_forms_are_not_guessed(recognition_site, tmp_path):
    server, base = recognition_site
    for kind in ("auth", "ambiguous"):
        session = RecognitionSession(base + "/recognize/" + kind, tmp_path / kind, headless=True)
        try:
            await session.start()
            result = await session.recognize()
            assert result.state == "LEARNING_REQUIRED"
            assert ("AUTH_REQUIRED" if kind == "auth" else "AMBIGUOUS_FORM") in result.diagnostics
            if kind == "ambiguous":
                session.select(result.forms[1].key)
                assert session.result.state == "DETECTED"
        finally:
            await session.close()
    assert not server.posts


async def test_learning_reused_without_secrets_or_false_http_claim(recognition_site, tmp_path):
    server, base = recognition_site
    knowledge = KnowledgeBase(tmp_path / "knowledge.sqlite")
    first = RecognitionSession(
        base + "/recognize/contenteditable", tmp_path / "first", knowledge=knowledge, headless=True
    )
    try:
        await first.start()
        await first.recognize()
        await first.test_publish("Private title", "Private body", "first", confirmed=True)
        assert first.result.state == "HTTP_CANDIDATE"
        rule_id = first.result.rule_id
        assert rule_id and knowledge.rules()[0].browser_successes == 1
        with serve_recognition_fixture() as (second_server, second_base):
            assert second_base != base
            untrained = RecognitionSession(
                second_base + "/recognize/theme-b", tmp_path / "untrained", headless=True
            )
            try:
                await untrained.start()
                assert (await untrained.recognize()).state == "LEARNING_REQUIRED"
            finally:
                await untrained.close()
            second = RecognitionSession(
                second_base + "/recognize/theme-b", tmp_path / "second", knowledge=knowledge, headless=True
            )
            try:
                await second.start()
                result = await second.recognize()
                assert result.state == "DETECTED" and result.rule_id == rule_id
                assert "learned-title-position" in result.forms[0].evidence
                assert not result.confidence.http_verified and result.confidence.result == 0
                assert not second_server.posts
                await second.test_publish("Second site", "New styling", "second", confirmed=True)
                assert second.result.state == "HTTP_CANDIDATE"
                verified = await second.verify_http("Other title", "HTTP on other site", confirmed=True)
                assert verified.status == "SUCCEEDED" and len(second_server.posts) == 2
                raw = json.dumps([r.model_dump() for r in knowledge.rules()])
                assert (
                    base not in raw
                    and second_base not in raw
                    and "Private" not in raw
                    and "cookies" not in raw
                )
            finally:
                await second.close()
    finally:
        await first.close()
        knowledge.close()


async def test_manual_learning_autofills_without_markers(recognition_site, tmp_path):
    server, base = recognition_site
    session = RecognitionSession(base + "/recognize/ajax", tmp_path / "profile", headless=True)
    try:
        await session.start()
        await session.recognize()
        await session.begin_manual("My title", "My body", confirmed=True)
        assert await session.page.locator('[name="subject"]').input_value() == "My title"
        await session.page.get_by_role("button", name="Enviar").click()
        await session.finish_manual("manual")
        assert session.result.state == "HTTP_CANDIDATE", session.result.summary()
        assert len(server.posts) == 1
        with pytest.raises(ValueError, match="ALREADY_ATTEMPTED"):
            await session.finish_manual("manual")
    finally:
        await session.close()


def test_engine_signatures_do_not_claim_workflow_support():
    name, confidence, evidence = EngineDetector().detect(
        '<meta name="generator" content="phpBB"><script src="/styles/prosilver/test.js"></script>'
    )
    assert name == "phpbb" and confidence == 0.6 and len(evidence) == 2
    assert EngineDetector().detect("<form><textarea></textarea></form>") == ("unknown", 0, [])


async def test_unknown_manual_demonstration_creates_reusable_rule(recognition_site, tmp_path):
    _, base = recognition_site
    knowledge = KnowledgeBase(tmp_path / "knowledge.sqlite")
    session = RecognitionSession(
        base + "/recognize/theme-b", tmp_path / "profile", knowledge=knowledge, headless=True
    )
    try:
        await session.start()
        assert (await session.recognize()).state == "LEARNING_REQUIRED"
        await session.begin_manual("Ordinary title", "Ordinary body", confirmed=True)
        await session.page.locator('[name="alpha"]').fill("Ordinary title")
        await session.page.locator('[contenteditable="true"]').fill("Ordinary body")
        await session.page.get_by_role("button", name="Enviar").click()
        await session.finish_manual("manual-unknown")
        assert session.result.state == "HTTP_CANDIDATE", session.result.summary()
        assert session.result.rule_id
        rule = knowledge.rules()[0]
        assert rule.source == "manual-observation" and "Ordinary" not in rule.model_dump_json()
        await session.close()
        new = RecognitionSession(
            base + "/recognize/theme-b", tmp_path / "new", knowledge=knowledge, headless=True
        )
        try:
            await new.start()
            assert (await new.recognize()).state == "DETECTED"
        finally:
            await new.close()
    finally:
        await session.close()
        knowledge.close()


async def test_survey_blocks_mutations_and_foreign_hosts(recognition_site, tmp_path):
    server, base = recognition_site
    session = RecognitionSession(base + "/recognize/ajax", tmp_path / "profile", headless=True)
    try:
        await session.start()
        await session.recognize()
        result = await session.page.evaluate("""async () => {
          try { await fetch('/v2/ajax', {method:'POST',headers:{'Content-Type':'application/json'},
                                      body:JSON.stringify({subject:'not authorized',message:'never'})}); return 'sent'; }
          catch { return 'blocked'; }
        }""")
        assert result == "blocked" and not server.write_counts
        foreign = await session.page.evaluate(
            "async () => {try {await fetch('https://foreign.invalid/collect');return 'sent';} catch{return 'blocked';}}"
        )
        assert foreign == "blocked" and "BLOCKED_HOST" in session.result.diagnostics
    finally:
        await session.close()
