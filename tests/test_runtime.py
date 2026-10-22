import gzip
import json

import httpx
import pytest
from pydantic import ValidationError

from poster.auth import encrypt_session
from poster.models import Step, Workflow
from poster.project import Project
from poster.runtime import Runtime, WorkflowError, render


@pytest.mark.parametrize("csrf", [False, True])
async def test_real_http_forms(fixture_site, make_project, csrf):
    server, base = fixture_site
    p = make_project(base, csrf=csrf)
    for n in range(2):
        r = await Runtime(p).run({"title": f"Title {n}", "body": "Текст & <tag>"})
        assert r.status == "SUCCEEDED", r
        assert "/posts/" in r.result_url
    assert len(server.posts) == 2
    if csrf:
        assert len(set(server.token_history)) == 2


async def test_stale_csrf_rejected(fixture_site):
    server, base = fixture_site
    async with httpx.AsyncClient() as c:
        import re

        old = re.search(r'name="csrf_token" value="([^"]+)"', (await c.get(base + "/csrf/new")).text)[1]
        await c.get(base + "/csrf/new")
        r = await c.post(base + "/csrf/posts", data={"subject": "A", "message": "B", "csrf_token": old})
        assert r.status_code == 403
    assert not server.posts


async def test_200_is_not_confirmation(make_project):
    seen = []

    async def handler(req):
        seen.append(req.method)
        return httpx.Response(200, text="OK")

    result = await Runtime(make_project("https://safe.test"), httpx.MockTransport(handler)).run(
        {"title": "A", "body": "B"}
    )
    assert result.status == "UNKNOWN"
    assert seen.count("POST") == 1


async def test_dry_run_never_sends_and_requires_variables(make_project):
    def forbidden(_):
        pytest.fail("Dry run made a request")

    p = make_project("https://safe.test", csrf=True)
    result = await Runtime(p, httpx.MockTransport(forbidden)).run({"title": "A", "body": "B"}, dry_run=True)
    assert result.status == "REVIEW_REQUIRED"
    failed = await Runtime(p).run({"title": "A"}, dry_run=True)
    assert failed.status == "FAILED"
    assert "body" in failed.reason


async def test_redirect_gate_prevents_cookie_and_header_leak(make_project):
    calls = []

    def handler(req):
        calls.append(req)
        assert req.url.host == "safe.test"
        return httpx.Response(302, headers={"Location": "https://foreign.test/collect?secret=private"})

    p = make_project("https://safe.test")
    state = encrypt_session(
        {
            "cookies": [{"name": "sid", "value": "private-cookie", "domain": "safe.test"}],
            "headers": {"Authorization": "Bearer private-token"},
        }
    )
    p = Project(p.site, p.workflow, p.revision, state)
    r = await Runtime(p, httpx.MockTransport(handler)).run({"title": "A", "body": "B"})
    assert r.status == "FAILED"
    assert len(calls) == 1
    assert calls[0].headers["authorization"] == "Bearer private-token"
    assert "private-cookie" in calls[0].headers["cookie"]
    assert "private" not in json.dumps(r.__dict__)


async def test_write_timeout_recovers_without_repeat(make_project):
    writes = []

    def handler(req):
        if req.method == "POST":
            writes.append(req.content)
            raise httpx.ReadTimeout("token=do-not-log", request=req)
        if req.url.path == "/lookup":
            return httpx.Response(200, json={"items": [{"title": "Once", "id": "42"}]})
        return httpx.Response(200, text="<form></form>")

    recovery = [
        {"id": "find", "op": "request", "url": "/lookup", "save_as": "found"},
        {
            "id": "confirm",
            "op": "verify",
            "source": "found",
            "kind": "json_path",
            "path": "items.0.title",
            "expected": "{{title}}",
            "result_url": True,
        },
    ]
    r = await Runtime(make_project("https://safe.test", recovery=recovery), httpx.MockTransport(handler)).run(
        {"title": "Once", "body": "B"}
    )
    assert r.status == "SUCCEEDED"
    assert len(writes) == 1
    assert "do-not-log" not in json.dumps(r.__dict__)


async def test_unknown_without_recovery_is_never_retried(make_project):
    writes = []

    def handler(req):
        if req.method == "POST":
            writes.append(1)
            raise httpx.ReadTimeout("timeout")
        return httpx.Response(200, text="OK")

    p = make_project("https://safe.test")
    r = await Runtime(p, httpx.MockTransport(handler)).run({"title": "A", "body": "B"})
    assert r.status == "UNKNOWN"
    assert len(writes) == 1
    recovered = await Runtime(p, httpx.MockTransport(handler)).run({}, recovery=True)
    assert recovered.status == "UNKNOWN"
    assert len(writes) == 1


async def test_auth_and_read_retries(make_project):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(503 if len(calls) == 1 else 401)

    p = make_project("https://safe.test", retry_delay=0)
    r = await Runtime(p, httpx.MockTransport(handler)).run({"title": "A", "body": "B"})
    assert r.status == "AUTH_EXPIRED"
    assert [r.method for r in calls] == ["GET", "GET"]


async def test_json_multipart_extract_set_assert_wait_and_gzip(make_project):
    calls = []

    def handler(req):
        calls.append(req)
        if req.url.path == "/json":
            assert json.loads(req.content) == {"title": "A"}
            return httpx.Response(
                200,
                content=gzip.compress(b'{"item":{"id":"99"}}'),
                headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
            )
        assert b'name="text"' in req.content
        assert b"99" in req.content
        return httpx.Response(200, text="<div id='ok'>99</div>")

    steps = [
        {"id": "set", "op": "set", "values": {"title": "A"}},
        {
            "id": "json",
            "op": "request",
            "method": "POST",
            "url": "/json",
            "json": {"title": "{{title}}"},
            "save_as": "r",
        },
        {
            "id": "extract",
            "op": "extract",
            "source": "r",
            "kind": "json_path",
            "path": "item.id",
            "save_as": "id",
        },
        {"id": "assert", "op": "assert", "source": "r", "kind": "status", "expected": 200},
        {"id": "wait", "op": "wait", "seconds": 0},
        {
            "id": "send",
            "op": "request",
            "method": "POST",
            "url": "/multi",
            "multipart": {"text": "{{id}}"},
            "save_as": "m",
        },
        {
            "id": "check",
            "op": "verify",
            "source": "m",
            "kind": "css_text",
            "selector": "#ok",
            "expected": "99",
        },
    ]
    r = await Runtime(make_project("https://safe.test", steps=steps), httpx.MockTransport(handler)).run({})
    assert r.status == "SUCCEEDED", r
    assert len(calls) == 2


def test_dsl_rejects_code_status_only_and_unsafe_recovery():
    with pytest.raises(ValidationError):
        Step(id="x", op="python", values={"code": "exit()"})
    with pytest.raises(ValidationError):
        Step(id="x", op="verify", source="page", kind="status", expected=200)
    with pytest.raises(ValidationError):
        Workflow(
            steps=[Step(id="v", op="verify", source="r", kind="contains", expected="ok")],
            recovery=[Step(id="post", op="request", method="POST", url="/x", save_as="x")],
        )
    with pytest.raises(WorkflowError, match="Missing variable"):
        render("{{missing}}", {})
