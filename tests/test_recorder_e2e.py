import subprocess
import sys


from poster.demo import demo
from poster.recorder.builder import analyze
from poster.recorder.capture import Capture


async def test_real_browser_to_http_demo(tmp_path):
    report = await demo(tmp_path / "demo")
    assert report["recorded_sites"] == ["classic", "csrf"]
    assert report["browser_writes"] == 2
    assert report["verified_http_writes"] == 2
    assert report["hot_load_without_restart"]
    assert report["restart_duplicate_writes"] == 0
    assert report["csrf_fresh_tokens"] == 2


def test_engine_has_no_browser_import_dependency():
    r = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import poster.engine; assert not any(x.startswith('playwright') for x in sys.modules)",
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr


def test_builder_unknown_workflow_is_not_http_ready():
    cap = Capture("https://example.test/new")
    result = analyze(cap, "new")
    assert result.site["status"] == "BROWSER_REQUIRED"
    assert result.diagnostics


def test_secret_field_and_missing_confirmation_require_review():
    cap = Capture("https://example.test/new")
    cap.requests = [
        {
            "method": "POST",
            "url": "https://example.test/post",
            "body": f"subject={cap.title_marker}&message={cap.body_marker}&token=never-export-me",
            "content_type": "application/x-www-form-urlencoded",
        }
    ]
    cap.forms = [{"url": cap.start_url, "action": "https://example.test/post", "fields": []}]
    result = analyze(cap, "review")
    assert result.site["status"] == "REVIEW"
    assert "never-export-me" not in str(result.workflow)


def test_builder_password_and_query_secrets_are_never_exported():
    cap = Capture("https://example.test/new?token=private-query-token")
    cap.requests = [
        {
            "method": "POST",
            "url": "https://example.test/post?auth=private-query-token",
            "body": f"subject={cap.title_marker}&message={cap.body_marker}&pwd=private-password",
            "content_type": "application/x-www-form-urlencoded",
        }
    ]
    cap.forms = [
        {
            "url": cap.start_url,
            "action": cap.requests[0]["url"],
            "fields": [{"name": "pwd", "type": "password"}],
        }
    ]
    result = analyze(cap, "review")
    assert result.site["status"] == "REVIEW"
    assert "private-query-token" not in str(result.workflow)
    assert "private-password" not in str(result.workflow)
