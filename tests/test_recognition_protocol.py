import json
import queue
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from pydantic import ValidationError

from poster.recognition.knowledge import Rule
from poster.recognition.network import NetworkAnalyzer, decode, rejected
from poster.recorder.capture import Capture
from poster.recorder.preferences import Preferences
from poster.runtime import render


def test_payload_decoding_handles_percent_encoding_and_json_escapes():
    cap = Capture("https://site.test/new", title_marker="Заголовок ✨", body_marker='Body "quote" & текст')
    for content_type, raw in [
        (
            "application/x-www-form-urlencoded",
            urlencode({"subject": cap.title_marker, "message": cap.body_marker}),
        ),
        (
            "application/json",
            json.dumps({"topic": {"subject": cap.title_marker, "message": cap.body_marker}}),
        ),
    ]:
        cap.requests = [
            {"method": "POST", "url": "https://site.test/send", "content_type": content_type, "body": raw}
        ]
        result = NetworkAnalyzer().analyze(cap)
        assert result.linked and not result.diagnostics


def test_separate_requests_need_correlation_and_dependency_not_just_time():
    cap = Capture("https://site.test/new", title_marker="Title", body_marker="Body", action_id="click-1")
    cap.requests = [
        {
            "id": "a",
            "action_id": "click-1",
            "time": 1,
            "method": "POST",
            "url": "/draft",
            "content_type": "application/json",
            "body": json.dumps({"subject": "Title"}),
        },
        {
            "id": "b",
            "action_id": "click-1",
            "time": 3,
            "method": "POST",
            "url": "/publish",
            "content_type": "application/json",
            "body": json.dumps({"message": "Body", "draft_id": 42}),
        },
    ]
    result = NetworkAnalyzer().analyze(cap)
    assert not result.linked and "UNLINKED_REQUESTS" in result.diagnostics
    cap.responses = [{"request_id": "a", "time": 2, "status": 200, "body": '{"draft":{"id":42}}'}]
    assert NetworkAnalyzer().analyze(cap).linked
    cap.requests[1]["body"] = json.dumps({"message": "Body", "draftId": 42})
    cap.responses[0]["body"] = '{"draftId":42}'
    assert NetworkAnalyzer().analyze(cap).linked
    cap.requests[1]["action_id"] = "another-action"
    result = NetworkAnalyzer().analyze(cap)
    assert not result.linked


def test_binary_multipart_and_unknown_encodings_are_not_silently_accepted():
    assert rejected({"status": 200, "body": '{"success":\n false,"url":"/old-post"}'})
    assert rejected({"status": 200, "body": '{"errors":["denied"]}'})
    assert not rejected({"status": 200, "body": '{"success":true}'})
    raw = '--b\r\nContent-Disposition: form-data; name="upload"; filename="a.txt"\r\n\r\nbytes\r\n--b--\r\n'
    with pytest.raises(ValueError):
        decode({"content_type": "multipart/form-data; boundary=b", "body": raw})
    with pytest.raises(ValueError):
        decode({"content_type": "application/octet-stream", "body": "binary"})


def test_closed_knowledge_schema_cannot_store_urls_or_credentials():
    with pytest.raises(ValidationError):
        Rule(
            id="https://secret.test/token",
            editor="textarea",
            roles=["title", "body"],
            source="browser-observation",
        )
    with pytest.raises(ValidationError):
        Rule(
            id="form-0123456789abcdef",
            editor="textarea",
            roles=["title", "body"],
            source="browser-observation",
            cookies={"sid": "secret"},
        )


def test_preferences_keep_last_url_but_strip_credentials_and_query(tmp_path):
    prefs = Preferences(tmp_path / "settings.json")
    prefs.save("https://user:password@example.test/new?token=secret#fragment", "my-project")
    assert prefs.load() == {"url": "https://example.test/new", "project_id": "my-project"}
    assert "secret" not in prefs.path.read_text()
    prefs.path.write_text("broken")
    assert prefs.load() == {}


def test_safe_template_filters_for_rich_text_and_query():
    assert render("{{body|html}}", {"body": "A & <b>"}) == "A &amp; &lt;b&gt;"
    assert render("{{title|url}}", {"title": "A & B"}) == "A%20%26%20B"
    assert render("{{id}}", {"id": 42}) == 42
    assert render("{{body}}", {"body": "Literal {{example}}"}) == "Literal {{example}}"


def test_ui_ignores_errors_from_previous_operation():
    from poster.recorder.app import App

    app = object.__new__(App)
    app.messages, app.generation = queue.Queue(), 2
    app.messages.put((1, "error", "STALE ERROR"))
    app.messages.put((2, "error", "Current error"))
    values = []
    app.status = SimpleNamespace(set=values.append)
    app.write = values.append
    app.update_buttons = lambda: None
    app.root = SimpleNamespace(after=lambda *_: None)
    app.pump()
    assert values == ["Current error", "Current error"]
