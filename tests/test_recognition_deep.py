import asyncio
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from poster.auth import decrypt_bytes, ensure_local_key
from poster.benchmark import corpus, run_case
from poster.fixtures_benchmark import serve_benchmark
from poster.recognition.detector import ADAPTERS, EngineDetector
from poster.recognition.knowledge import FormKnowledge, KnowledgeBase, Rule
from poster.recognition.navigation import NavigationDiscovery
from poster.recognition.network import decode
from poster.recognition.policy import ResourcePolicy, SubmissionScope
from poster.recognition.types import FieldRef, FormCandidate
from poster.recorder.capture import Capture, Recorder


@pytest.fixture(scope="module")
def broad_site():
    with serve_benchmark(corpus()) as site:
        yield site


@pytest.mark.parametrize("case", corpus(), ids=lambda c: c["id"])
async def test_heldout_browser_to_http(case, broad_site, tmp_path):
    server, base = broad_site
    result = await run_case(case, base, tmp_path)
    assert result["form"] and result["fields"], result
    negative = case.get("negative")
    assert not result["false_confirmation"]
    if negative in {"rejected", "silent", "hidden", "login"}:
        assert not result["browser"] and not result["http"], result
    elif negative == "opaque":
        assert result["browser"] and not result["http"] and result["state"] == "LEARNING_REQUIRED", result
    else:
        assert result["browser"] and result["http"], result
    assert not any(p.endswith("/side-effect") for p in server.writes)


def candidate(index=0, editor="textarea", count=2, layout=("light", "inline")):
    refs = [
        FieldRef(
            "title",
            f"field-{i}",
            confidence=0.3,
            position=i,
            features=["single-line", "before-editor" if i == index else "after-editor"],
        )
        for i in range(count)
    ]
    return FormCandidate(
        "sample",
        {
            "title": refs[index],
            "body": FieldRef("body", "body", kind=editor, confidence=0.8),
            "submit": FieldRef("submit", "button", kind="button"),
        },
        0.95,
        text_inputs=refs,
        structure=list(layout),
    )


def test_migration_export_import_backup_and_closed_schema(tmp_path):
    path = tmp_path / "old.sqlite"
    rule = Rule(
        id="form-0123456789abcdef",
        version=1,
        editor="textarea",
        roles=["title", "body", "submit"],
        source="browser-observation",
        browser_successes=1,
    )
    legacy = rule.model_dump(include={"id", "version", "editor", "roles", "source", "browser_successes"})
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE recognition_rules(id TEXT PRIMARY KEY,rule TEXT NOT NULL)")
        db.execute("INSERT INTO recognition_rules VALUES(?,?)", (rule.id, json.dumps(legacy)))
    kb = KnowledgeBase(path)
    assert kb.db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert kb.rules()[0].browser_successes == 1
    kb.export_rules(tmp_path / "safe.json")
    kb.backup(tmp_path / "backup.sqlite")
    with pytest.raises(FileExistsError):
        kb.backup(tmp_path / "backup.sqlite")
    kb.close()
    kb = KnowledgeBase(tmp_path / "backup.sqlite")
    assert len(kb.rules()) == 1
    kb.close()
    imported = KnowledgeBase(tmp_path / "import.sqlite")
    assert imported.import_rules(tmp_path / "safe.json") == 1
    assert imported.import_rules(tmp_path / "safe.json") == 0
    assert imported.rules()[0].maturity == "observed" and imported.rules()[0].http_successes == 0
    data = json.loads((tmp_path / "safe.json").read_text())
    data["rules"][0]["tokens"]["patterns"] = [{"name": "cookieSecret", "role": "csrf", "source": "json"}]
    (tmp_path / "bad.json").write_text(json.dumps(data))
    with pytest.raises(ValidationError):
        imported.import_rules(tmp_path / "bad.json")
    assert len(imported.rules()) == 1
    imported.close()


def test_relational_transfer_conflict_priority_and_quarantine(tmp_path):
    kb = KnowledgeBase(tmp_path / "kb.sqlite")
    a = candidate(index=0)
    first = kb.propose(a, origin="installation-A")
    kb.result(first, True)
    b = candidate(index=1)
    b.fields.pop("title")
    assert kb.suggest(b).id == first
    assert b.fields["title"].position == 1  # Relation survives reordering.
    second = kb.propose(candidate(index=1), origin="installation-B")
    c = candidate(index=0)
    # Remove relational clues to produce genuinely conflicting positional rules.
    for r in kb.rules():
        r.form = FormKnowledge(input_count=2)
        kb.save(r)
    c.fields.pop("title")
    assert kb.suggest(c) is None and "knowledge-conflict" in c.evidence
    kb.disable(second)
    assert kb.suggest(c).id == first
    kb.result(first, False)
    kb.result(first, False)
    d = candidate()
    d.fields.pop("title")
    assert kb.suggest(d) is None
    assert kb.rules()[0].failures >= 0
    kb.close()


def test_knowledge_six_categories_from_observation(tmp_path):
    from poster.recognition.learner import HTTPWorkflowLearner

    cap = Capture(
        "https://example.test/new",
        title_marker="Unique subject",
        body_marker="Unique body",
        action_id="one",
        browser_verified=True,
        confirmation={"url_path": "url", "title_selector": "article h1", "body_selector": "article p"},
    )
    cap.requests = [
        {
            "id": "a",
            "action_id": "one",
            "time": 1,
            "method": "POST",
            "url": "https://example.test/send",
            "content_type": "application/json",
            "body": json.dumps(
                {"subject": cap.title_marker, "message": cap.body_marker, "csrf": "secret-token"}
            ),
        }
    ]
    cap.responses = [
        {
            "request_id": "p",
            "method": "GET",
            "time": 0,
            "url": cap.start_url,
            "status": 200,
            "body": '<meta name="csrf" content="secret-token">',
        }
    ]
    kb = KnowledgeBase(tmp_path / "kb.sqlite")
    rid = kb.propose(
        candidate(),
        origin="https://example.test",
        engine="phpbb",
        navigation=[{"purpose": "compose", "score": 0.9, "evidence": ["compose-route", "navigation-link"]}],
        capture=cap,
    )
    build = HTTPWorkflowLearner().learn(cap, "candidate")
    kb.observe_workflow(rid, build.workflow)
    r = kb.rules()[0]
    assert r.engine.family == "phpbb" and r.navigation.steps
    assert r.submission.operations[0].encoding == "json" and r.tokens.patterns[0].source == "html-meta"
    assert r.verification.permalink == "json" and r.form.title_features
    raw = r.model_dump_json()
    assert all(
        s not in raw for s in ("secret-token", "Unique subject", "example.test", "article h1", "field-0")
    )
    kb.export_rules(tmp_path / "export.json")
    assert "https://" not in (tmp_path / "export.json").read_text()
    kb.close()


@pytest.mark.parametrize(
    "charset,value", [("windows-1251", "Тема"), ("iso-8859-1", "débat"), ("utf-8", "موضوع")]
)
def test_declared_charsets(charset, value):
    from urllib.parse import urlencode

    raw = urlencode({"subject": value}, encoding=charset)
    assert (
        decode({"content_type": f"application/x-www-form-urlencoded; charset={charset}", "body": raw})[1][
            "subject"
        ]
        == value
    )


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.id)
def test_engine_channels_and_false_positives(adapter):
    # A repeated asset signature is still one channel, never proof of a CMS.
    probes = {
        "xenforo": ("/js/xf/core.js", "/threads/demo/"),
        "phpbb": ("/styles/prosilver/style.css", "posting.php"),
        "vbulletin": ("/clientscript/vbulletin.js", "newthread.php"),
        "mybb": ("/jscripts/mybb.js", "newthread.php"),
        "discourse": ("/assets/discourse-application.js", "/t/demo/1"),
        "smf": ("/Themes/default/scripts/script.js", "index.php?action=post"),
        "invision": ("/applications/core/app.js", "/topic/demo/"),
        "flarum": ("/assets/forum-abc.js", "/d/1"),
        "wpforo": ("/wp-content/plugins/wpforo/style.css", "/community/"),
        "wordpress": ("/wp-content/style.css", "/wp-login.php"),
    }
    asset, route = probes[adapter.id]
    detector = EngineDetector()
    only = f'<script src="{asset}"></script>' * 5
    assert not detector.inspect(only)
    both = (
        only
        + f'<meta name="generator" content="{adapter.generator.split("|")[0]} 3.3.15"><a href="{route}">Forum</a>'
    )
    found = detector.inspect(both)
    assert any(t.family == adapter.id and len(t.evidence) >= 2 for t in found)
    assert all(
        t.capabilities == ("identification",)
        or t.family in {"phpbb", "mybb", "smf", "flarum"}
        and t.tested_versions
        for t in found
    )


def test_wordpress_plus_wpforo_and_content_decoy():
    detector = EngineDetector()
    report = detector.inspect(
        '<meta name="generator" content="WordPress 6.8"><script src="/wp-includes/wp.js"></script><div id="wpforo"><link href="/wp-content/plugins/wpforo/style.css"></div>'
    )
    assert {t.family for t in report} == {"wordpress", "wpforo"}
    assert (
        detector.detect("<article>Users discuss phpBB /styles/prosilver/ posting.php XenForo</article>")[0]
        == "unknown"
    )


@pytest.mark.parametrize(
    "node",
    [
        {"tag": "button", "type": "submit", "text": "New Topic"},
        {"tag": "button", "type": "button", "text": "Unlabelled", "attrs": "pencil"},
        {"tag": "a", "href": "/logout", "text": "New Topic"},
        {"tag": "a", "href": "https://evil.invalid/new-topic", "text": "New Topic"},
        {"tag": "button", "type": "button", "text": "Delete", "target_editor": True},
    ],
)
def test_unknown_or_dangerous_navigation_not_clicked(node):
    assert not NavigationDiscovery().classify({"selector": "test", **node}, "https://forum.test").safe


async def test_response_keeps_original_action_after_async_delay(tmp_path):
    class Context:
        handlers = {}

        async def expose_binding(self, *args):
            pass

        async def add_init_script(self, *args):
            pass

        def on(self, name, callback):
            self.handlers[name] = callback

    context = Context()
    recorder = Recorder("https://forum.test/new", tmp_path)
    await recorder.attach(context)

    class Req:
        resource_type = "fetch"
        post_data_buffer = b"{}"
        headers = {"content-type": "application/json"}
        redirected_from = None
        method = "POST"
        url = "https://forum.test/send"
        frame = SimpleNamespace(url="https://forum.test/new")

    req = Req()
    recorder.capture.action_id = "original"
    context.handlers["request"](req)
    event = asyncio.Event()

    async def delayed_text():
        await event.wait()
        return "{}"

    res = SimpleNamespace(request=req, text=delayed_text, url=req.url, status=200, headers={})
    context.handlers["response"](res)
    await asyncio.sleep(0)
    recorder.capture.action_id = "next-action"
    other = Req()
    context.handlers["request"](other)
    event.set()
    await asyncio.gather(*recorder.pending)
    assert recorder.capture.responses[0]["action_id"] == "original"
    assert recorder.capture.responses[0]["request_id"] == "r0"
    assert recorder.request_records[req]["id"] == "r0" and recorder.request_records[other]["id"] == "r1"


def test_legacy_and_current_keys_remain_usable_without_overwrite(tmp_path, monkeypatch):
    import poster.auth as auth

    monkeypatch.delenv("POSTER_SESSION_KEY", raising=False)
    monkeypatch.setattr(auth, "_legacy_keys", [])
    previous, current = Fernet.generate_key(), Fernet.generate_key()
    legacy = tmp_path / "home/.poster"
    legacy.mkdir(parents=True)
    (legacy / "session.key").write_bytes(previous)
    private = tmp_path / "private"
    private.mkdir()
    (private / "session.key").write_bytes(current)
    ensure_local_key(private, legacy_home=tmp_path / "home")
    for key in (previous, current):
        assert decrypt_bytes(Fernet(key).encrypt(b'{"cookies":[]}')) == {"cookies": []}
    assert (legacy / "session.key").read_bytes() == previous and (
        private / "session.key"
    ).read_bytes() == current


async def test_static_fetch_does_not_forward_secrets_or_accept_redirect_escape(monkeypatch):
    seen = []

    def respond(request):
        seen.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "https://elsewhere.invalid/private"})
        return httpx.Response(
            200, headers={"content-type": "text/css", "set-cookie": "leak=secret"}, text="body{}"
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs)
    )
    request = SimpleNamespace(
        url="https://assets.test/style.css",
        method="GET",
        resource_type="stylesheet",
        headers={
            "cookie": "secret",
            "authorization": "secret",
            "referer": "https://private.test/?token=secret",
        },
    )
    filled = []

    class Route:
        async def fulfill(self, **kwargs):
            filled.append(kwargs)

    route = Route()
    route.request = request
    policy = ResourcePolicy(["https://assets.test"])
    assert await policy.fulfill_static(route)
    assert all(k not in seen[0].headers for k in ("cookie", "authorization", "referer"))
    assert "set-cookie" not in filled[0]["headers"]
    request.url = "https://assets.test/style.css?token=private"
    assert not await policy.fulfill_static(route)
    request.url = "https://assets.test/redirect"
    assert not await policy.fulfill_static(route)
    assert len(seen) == 2 and all(r.url.host == "assets.test" for r in seen)


def test_scope_blocks_background_duplicate_foreign_and_unlinked_mutations():
    cap = Capture("https://forum.test/new", title_marker="Consent title", body_marker="Consent body")
    scope = SubmissionScope(cap.start_url, cap)

    def req(url, body, method="POST"):
        return SimpleNamespace(
            url=url,
            method=method,
            post_data_buffer=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )

    assert not scope.allows(req("https://forum.test/settings", {"enabled": True}))
    assert not scope.allows(req("https://forum.test:9443/send", {"subject": cap.title_marker}))
    permitted = req("https://forum.test/send", {"subject": cap.title_marker, "message": cap.body_marker})
    assert scope.allows(permitted)
    assert not scope.allows(permitted)
    assert not scope.allows(req("https://forum.test/other", {"subject": cap.title_marker}))
    assert not scope.allows(req("https://forum.test/delete", {"subject": cap.title_marker}, "DELETE"))


async def test_relational_learning_transfers_after_restart_language_and_dom_changes(tmp_path):
    from poster.recognition.service import RecognitionSession

    training = dict(
        id="training",
        locale="en",
        editor="textarea",
        nav="menu",
        protocol="draft",
        layout="classic",
        reorder=True,
        stratum="training",
    )
    heldout = dict(
        id="transfer",
        locale="ar",
        editor="textarea",
        nav="icon",
        protocol="header",
        layout="classic",
        unknown=True,
        aux_first=True,
        stratum="trained",
    )
    kb_path = tmp_path / "knowledge.sqlite"
    kb = KnowledgeBase(kb_path)
    with serve_benchmark([training]) as (_, base):
        a = RecognitionSession(base + "/bench/training", tmp_path / "a", knowledge=kb, headless=True)
        try:
            await a.start()
            await a.recognize()
            await a.test_publish("Training subject", "Training message", "training", confirmed=True)
            assert a.result.state == "HTTP_CANDIDATE", a.result.summary()
            assert (
                await a.verify_http("Training HTTP subject", "Training HTTP message", confirmed=True)
            ).status == "SUCCEEDED"
        finally:
            await a.close()
    kb.close()
    with serve_benchmark([heldout]) as (_, base):
        b = RecognitionSession(base + "/bench/transfer", tmp_path / "b", headless=True)
        try:
            await b.start()
            assert (await b.recognize()).state == "LEARNING_REQUIRED"
        finally:
            await b.close()
        kb = KnowledgeBase(kb_path)
        trained = RecognitionSession(
            base + "/bench/transfer", tmp_path / "trained", knowledge=kb, headless=True
        )
        try:
            await trained.start()
            assert (await trained.recognize()).state == "DETECTED", trained.result.summary()
            assert (
                next(f for f in trained.result.forms if f.key == trained.result.selected)
                .fields["title"]
                .position
                == 1
            )
            assert not trained.result.confidence.http_verified
            await trained.test_publish("عنوان جديد", "محتوى جديد", "transfer", confirmed=True)
            assert trained.result.state == "HTTP_CANDIDATE", trained.result.summary()
            await trained.close()
            assert (
                await trained.verify_http("عنوان HTTP جديد", "محتوى HTTP مستقل", confirmed=True)
            ).status == "SUCCEEDED"
            assert any(r.submission.operations and r.tokens.patterns for r in kb.rules())
            assert len(kb.rules()) >= 2  # New variant preserves the old rule.
        finally:
            await trained.close()
            kb.close()


def test_cookie_numeric_coincidence_does_not_remove_forum_parameter():
    from poster.recognition.learner import HTTPWorkflowLearner

    cap = Capture(
        "https://forum.test/new?f=2&sid=private-session",
        title_marker="Observed title",
        body_marker="Observed body",
        action_id="a",
        browser_verified=True,
        confirmation={"title_selector": "h1", "body_selector": "article"},
    )
    cap.session = {
        "cookies": [{"name": "forum_user", "value": "2"}, {"name": "forum_sid", "value": "private-session"}]
    }
    cap.requests = [
        {
            "id": "w",
            "action_id": "a",
            "method": "POST",
            "url": "https://forum.test/send?f=2&sid=private-session",
            "content_type": "application/json",
            "body": json.dumps({"subject": cap.title_marker, "message": cap.body_marker}),
        }
    ]
    build = HTTPWorkflowLearner().learn(cap, "cookies")
    assert build.workflow["steps"][0]["url"] == "https://forum.test/new?f=2"
    raw = json.dumps(build.workflow)
    assert "private-session" not in raw and "f=2" in raw and '"cookie"' in raw


def test_unexplained_numeric_identifier_blocks_http_readiness():
    from poster.recognition.learner import HTTPWorkflowLearner

    cap = Capture(
        "https://forum.test/new",
        title_marker="Observed title",
        body_marker="Observed body",
        action_id="a",
        browser_verified=True,
        confirmation={"title_selector": "h1", "body_selector": "article"},
    )
    cap.requests = [
        {
            "id": "w",
            "action_id": "a",
            "method": "POST",
            "url": "https://forum.test/send",
            "content_type": "application/json",
            "body": json.dumps({"subject": cap.title_marker, "message": cap.body_marker, "draft_id": 7}),
        }
    ]
    build = HTTPWorkflowLearner().learn(cap, "unknown-id")
    assert (
        build.site["recognition"]["state"] == "LEARNING_REQUIRED" and "UNKNOWN_DYNAMIC" in build.diagnostics
    )


async def test_encrypted_auth_preserves_user_agent_and_cookie_header_extraction(make_project):
    from poster.auth import encrypt_session
    from poster.models import Step, Workflow
    from poster.project import Project
    from poster.runtime import Runtime

    project = make_project("https://forum.test")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            headers={"X-CSRF-Token": "fresh", "set-cookie": "sid=fresh-cookie; Path=/"},
            text="<h1>Verified title</h1><p>Verified body</p>",
        )

    workflow = Workflow(
        steps=[
            Step(id="read", op="request", url="/view", save_as="r"),
            Step(
                id="token", op="extract", source="r", kind="header", attribute="x-csrf-token", save_as="token"
            ),
            Step(id="cookie", op="extract", source="r", kind="cookie", attribute="sid", save_as="sid"),
            Step(
                id="assert_cookie",
                op="assert",
                source="r",
                kind="cookie",
                attribute="sid",
                expected="fresh-cookie",
            ),
            Step(
                id="verify",
                op="verify",
                source="r",
                kind="css_text",
                selector="h1",
                expected="Verified title",
            ),
        ]
    )
    auth = encrypt_session({"cookies": [], "headers": {"user-agent": "Browser session UA"}})
    trial = Runtime(
        Project(project.site, workflow, "test", auth),
        transport=httpx.MockTransport(handler),
        strict_origin=True,
    )
    assert (await trial.run({})).status == "SUCCEEDED"
    assert seen[0].headers["user-agent"] == "Browser session UA"
    from poster.runtime import WorkflowError

    with pytest.raises(WorkflowError, match="origin"):
        trial.check_url("https://forum.test:9443/receive")
