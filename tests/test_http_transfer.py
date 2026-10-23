"""Cross-installation recipe rebinding; these are protocol tests, not CMS claims."""

import copy
import json
from types import SimpleNamespace

import pytest

from poster.benchmark import corpus
from poster.fixtures_benchmark import serve_benchmark
from poster.recognition.knowledge import Rule
from poster.recognition.learner import HTTPWorkflowLearner
from poster.recognition.network import decode
from poster.recognition.policy import SubmissionScope
from poster.recognition.service import RecognitionSession
from poster.recognition.transfer import compile_template, instantiate
from poster.recorder.capture import Capture
from poster.runtime import render, result_permalink


def observation(origin="https://a.test", *, renamed=False):
    token = "b" * 32 if renamed else "a" * 32
    cap = Capture(
        origin + "/new",
        title_marker="Subject Ж",
        body_marker="Body & Ж",
        action_id="one",
        browser_verified=True,
        confirmation={"url_path": "url", "title_selector": "h1", "body_selector": "article"},
    )
    cap.responses = [
        {
            "request_id": "page",
            "method": "GET",
            "status": 200,
            "url": cap.start_url,
            "body": f'<meta name="{"local-proof" if renamed else "csrf"}" content="{token}">',
            "time": 0,
        }
    ]
    cap.requests = [
        {
            "id": "w",
            "action_id": "one",
            "time": 1,
            "method": "POST",
            "url": origin + "/topics",
            "content_type": "application/json",
            "body": json.dumps(
                {
                    "subject": cap.title_marker,
                    "message": cap.body_marker,
                    "vouch" if renamed else "csrf": token,
                }
            ),
        }
    ]
    return cap


def rule(template):
    return Rule(
        id="form-0123456789abcdef",
        editor="textarea",
        roles=["title", "body", "submit"],
        source="browser-observation",
        http_templates=[template],
    )


def test_real_template_rebinds_renamed_token_without_carrying_site_data():
    a = observation()
    old = HTTPWorkflowLearner().learn(a, "a")
    template = compile_template(a, old.workflow)
    assert len(template.bindings) == 1
    encoded = template.model_dump_json()
    assert all(s not in encoded for s in ("a.test", "csrf", "local-proof", "a" * 32, "Subject", "h1"))
    b = observation("https://b.test", renamed=True)
    cold = HTTPWorkflowLearner().learn(b, "b")
    assert cold.site["recognition"]["state"] == "LEARNING_REQUIRED"
    learner = HTTPWorkflowLearner()
    warm = learner.learn(b, "b", knowledge_rule=rule(template))
    assert warm.site["recognition"]["state"] == "HTTP_CANDIDATE"
    assert learner.transfer_report == {"applied": True, "operations": 1, "bindings": 1}
    assert "a.test" not in json.dumps(warm.workflow) and "b" * 32 not in json.dumps(warm.workflow)
    assert any(s.get("selector") == 'meta[name="local-proof"]' for s in warm.workflow["steps"])
    assert not warm.site["recognition"]["http_verified"]
    # A local missing/ambiguous source must stop transfer, not reuse A's token.
    b.responses[0]["body"] += b.responses[0]["body"]
    assert instantiate(template, b).diagnostics == ["TRANSFER_SOURCE_MISSING"]


def graph(origin="https://a.test", *, renamed=False):
    cap = observation(origin)
    identifier = "x" * 24 if renamed else "z" * 24
    cap.requests[0]["url"] = origin + "/draft"
    cap.responses.append(
        {
            "request_id": "w",
            "action_id": "one",
            "method": "POST",
            "status": 200,
            "url": cap.requests[0]["url"],
            "time": 2,
            "body": json.dumps({"handle" if renamed else "draft_id": identifier}),
        }
    )
    cap.requests.append(
        {
            "id": "commit",
            "action_id": "one",
            "time": 3,
            "method": "POST",
            "url": origin + "/publish/" + identifier,
            "content_type": "application/json",
            "body": json.dumps({"opaque" if renamed else "draft_id": identifier}),
        }
    )
    return cap


def test_dependency_graph_reused_for_opaque_payload_and_path_and_browser_scope():
    a = graph()
    old = HTTPWorkflowLearner().learn(a, "a")
    template = compile_template(a, old.workflow)
    assert {b.target_kind for b in template.bindings} >= {"payload", "path"}
    b = graph("https://b.test", renamed=True)
    cold = HTTPWorkflowLearner().learn(b, "b")
    assert cold.site["recognition"]["state"] == "LEARNING_REQUIRED"
    learner = HTTPWorkflowLearner()
    candidate = learner.learn(b, "b", knowledge_rule=rule(template))
    assert learner.transfer_report["operations"] == 2
    assert candidate.site["recognition"]["state"] == "HTTP_CANDIDATE"
    assert "x" * 24 not in json.dumps(candidate.workflow)
    final = next(s for s in candidate.workflow["steps"] if s["id"] == "write_1")
    assert "|url}}" in final["url"] and "{{" in final["json"]["opaque"]
    # The same graph also constrains browser admission of the opaque follow-up.
    scope = SubmissionScope("https://b.test", b, templates=[template])
    first = b.requests[0]

    def request(r):
        return SimpleNamespace(
            method=r["method"],
            url=r["url"],
            headers={"content-type": r["content_type"]},
            post_data_buffer=r["body"].encode(),
        )

    assert scope.allows(request(first))
    scope.allowed_ids.add("w")
    assert not scope.allows(request({**b.requests[1], "url": "https://b.test/analytics"}))
    assert scope.allows(request(b.requests[1]))
    assert not scope.allows(request(b.requests[1]))
    broken = copy.deepcopy(b)
    broken.responses[1]["time"] = 4  # Not a preceding response.
    assert instantiate(template, broken).diagnostics


def test_native_submitter_scope_blocks_content_bearing_background_drafts():
    cap = observation()
    endpoint = cap.requests[0]["url"]
    scope = SubmissionScope(endpoint, cap, submitter=("post", "Publish"), action_url=endpoint)

    def request(**extra):
        return SimpleNamespace(
            method="POST",
            url=endpoint,
            headers={"content-type": "application/json"},
            post_data_buffer=json.dumps(
                {"subject": cap.title_marker, "message": cap.body_marker, **extra}
            ).encode(),
        )

    assert not scope.allows(request(save_draft="1"))
    assert not scope.sent
    assert scope.allows(request(post="Publish"))


def test_a_template_for_another_operation_does_not_authorize_full_content_drafts():
    from poster.recognition.transfer import HTTPTemplate, Operation, shape

    cap = Capture("https://a.test/new", title_marker="Topic title", body_marker="Post body")
    payload = {"subject": cap.title_marker, "message": cap.body_marker}
    operation = Operation(
        method="POST", encoding="json", roles=["body", "title"], shape=shape(payload), route="publish"
    )
    req = SimpleNamespace(
        url="https://a.test/draft",
        method="POST",
        post_data_buffer=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "x-poster-interaction": "click"},
    )
    scope = SubmissionScope(
        cap.start_url, cap, interaction="click", templates=[HTTPTemplate(operations=[operation])]
    )
    assert not scope.allows(req) and not scope.sent
    proven = operation.model_copy(update={"route": "draft"})
    scope = SubmissionScope(
        cap.start_url, cap, interaction="click", templates=[HTTPTemplate(operations=[proven])]
    )
    assert scope.allows(req)


def test_empty_file_controls_and_identical_scalar_multipart_values():
    empty = '--b\r\nContent-Disposition: form-data; name="file[]"; filename=""\r\n\r\n\r\n'
    scalar = '--b\r\nContent-Disposition: form-data; name="MAX_FILE_SIZE"\r\n\r\n100\r\n'
    assert decode(
        {"content_type": "multipart/form-data; boundary=b", "body": empty + scalar + scalar + "--b--\r\n"}
    )[1] == {"MAX_FILE_SIZE": "100"}
    with pytest.raises(ValueError):
        decode(
            {
                "content_type": "multipart/form-data; boundary=b",
                "body": scalar + scalar.replace("100", "200") + "--b--\r\n",
            }
        )


def test_safe_multiline_html_and_useful_redacted_forum_permalinks():
    assert render("{{body|html_lines}}", {"body": "<tag>\nЖ &"}) == "&lt;tag&gt;<br>Ж &amp;"
    assert (
        result_permalink("https://x.test/index.php?topic=12.msg13;sid=SECRET#msg13")
        == "https://x.test/index.php?topic=12.0"
    )
    assert (
        result_permalink("https://x.test/showthread.php?tid=4&token=SECRET")
        == "https://x.test/showthread.php?tid=4"
    )


def test_inline_json_nonce_sources_are_fresh_and_never_execute_javascript():
    from poster.recognition.javascript import token_slots

    assert (
        result_permalink("https://x.test/index.php?topic=12.new#new") == "https://x.test/index.php?topic=12.0"
    )

    cap = observation()
    cap.responses[0]["body"] = '<script>var ajaxConfig={"nonce":"abcdefgh12"};</script>'
    payload = json.loads(cap.requests[0]["body"])
    payload["csrf"] = "abcdefgh12"
    cap.requests[0]["body"] = json.dumps(payload)
    built = HTTPWorkflowLearner().learn(cap, "inline")
    assert built.site["recognition"]["state"] == "HTTP_CANDIDATE"
    assert "abcdefgh12" not in json.dumps(built.workflow)
    template = compile_template(cap, built.workflow)
    assert template.bindings[0].kind == "regex"
    cap.responses[0]["body"] = '<script>var ajaxConfig={"rotated":"abcdefgh34"};</script>'
    payload["csrf"] = "abcdefgh34"
    cap.requests[0]["body"] = json.dumps(payload)
    assert (
        HTTPWorkflowLearner()
        .learn(cap, "rotated", knowledge_rule=rule(template))
        .site["recognition"]["state"]
        == "HTTP_CANDIDATE"
    )
    assert not token_slots('<script>var nonce=decrypt("abcdefgh12");</script>')
    assert not token_slots('<script>var a={"nonce":"abcdefgh12"};var b={"nonce":"abcdefgh12"};</script>')


@pytest.mark.parametrize("timeout,expected", [(None, "HTTP_CANDIDATE"), (0.3, "UNKNOWN")])
async def test_delayed_publication_over_four_seconds_is_not_repeated(timeout, expected, tmp_path):
    case = {**corpus()[0], "id": "long-response", "negative": "slow", "response_delay": 5.1}
    with serve_benchmark([case]) as (server, base):
        session = RecognitionSession(
            base + "/bench/long-response", tmp_path / "profile", headless=True, confirmation_timeout=timeout
        )
        try:
            await session.start()
            await session.recognize()
            await session.test_publish("Delayed title", "Delayed body", "delayed", confirmed=True)
            assert session.result.state == expected, session.result.summary()
            assert len(server.writes) == 1
            if expected == "UNKNOWN":
                assert "CONFIRMATION_PENDING" in session.result.diagnostics
                with pytest.raises(ValueError, match="ALREADY_ATTEMPTED"):
                    await session.test_publish("Again", "Again body", "delayed", confirmed=True)
        finally:
            await session.close()
