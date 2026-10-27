import asyncio
from pathlib import Path

import pytest

from poster.fixtures_readiness import serve_readiness
from poster.recognition.service import RecognitionSession
from poster.recognition.policy import ResourcePolicy


@pytest.mark.parametrize(
    "case",
    ["topic", "reply", "comment", "ajax", "background", "synchronous", "navigation", "listener", "toolbar"],
)
async def test_independent_shapes_complete_export_restart(case, tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "acceptance", Path(__file__).parents[1] / "scripts/check_windows_e2e.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = await module.check(tmp_path / case, case)
    assert report["writes"] == 3 and report["runtime_new_process"]


@pytest.mark.parametrize(
    "case,detected",
    [("ambiguous", False), ("unsafe", False), ("delayed", True), ("mismatch", True), ("format-only", False)],
)
async def test_ambiguous_unsafe_and_deferred_click_are_not_mutations(case, detected, tmp_path):
    with serve_readiness() as (server, base):
        s = RecognitionSession(
            base + "/compose/" + case, tmp_path / "profile", headless=True, confirmation_timeout=0.5
        )
        try:
            await s.start()
            await s.recognize()
            assert (s.result.state == "DETECTED") == detected, s.result.summary()
            if detected:
                await s.test_publish("Unfamiliar", "Текст публикации", "probe", confirmed=True)
                assert s.result.state == "FAILED"
                with pytest.raises(ValueError, match="ALREADY_ATTEMPTED"):
                    await s.test_publish("Again", "Other", "probe", confirmed=True)
            assert not server.writes
        finally:
            await s.close()


async def test_static_ssrf_and_redirect_query_validation(monkeypatch):
    p = ResourcePolicy(["https://static.example"])
    assert not p.safe_url("https://static.example/style.css?v=1")
    assert p.safe_url("https://static.example/version-1/style.css")
    assert not p.safe_url("https://static.example/style.css?access%5Ftoken=secret")
    assert not p.safe_url("https://static.example/style.css?%2574oken=secret")
    assert not p.safe_url("https://static.example@127.0.0.1/x")
    with pytest.raises(ValueError):
        ResourcePolicy(["http://127.0.0.1"])

    for address in ("127.0.0.1", "169.254.169.254", "224.0.0.1", "::1"):

        async def lookup(*args, _address=address, **kwargs):
            return [(2, 1, 6, "", (_address, 443))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
        with pytest.raises(ValueError, match="private"):
            await p.public_addresses("static.example", 443)


async def test_disappeared_control_and_secret_free_diagnostics(tmp_path):
    with serve_readiness() as (server, base):
        s = RecognitionSession(base + "/compose/topic", tmp_path / "profile", headless=True)
        try:
            await s.start()
            await s.recognize()
            await s.page.locator("button").evaluate("e=>e.remove()")
            await s.test_publish("Title", "Private content", "gone", confirmed=True)
            assert s.result.state == "FAILED" and "BUTTON_DISAPPEARED" in s.result.diagnostics
            assert s.read_only and not server.writes
            s.diagnose("NETWORK_FAILED", ValueError("password=PRIVATE&token=SECRET"))
            s.diagnose("CLICK_FAILED", TimeoutError("element is not visible; password=PRIVATE&token=SECRET"))
            log = (tmp_path / "diagnostics.jsonl").read_text()
            assert "PRIVATE" not in log and "SECRET" not in log and "http" not in log
            assert "NOT_VISIBLE" in log
        finally:
            await s.close()


async def test_blocked_unknown_cdn_is_explained(tmp_path):
    with serve_readiness() as (_, base):
        s = RecognitionSession(base + "/compose/cdn", tmp_path / "profile", headless=True)
        try:
            await s.start()
            await s.recognize()
            assert "STATIC_RESOURCE_BLOCKED" in s.result.diagnostics
            assert "STATIC_ORIGIN_UNAPPROVED" in s.result.diagnostics
        finally:
            await s.close()


async def test_stop_closes_all_mutation_permissions(tmp_path):
    from poster.recognition.policy import SubmissionScope

    with serve_readiness() as (server, base):
        s = RecognitionSession(base + "/compose/background", tmp_path / "profile", headless=True)
        await s.start()
        await s.recognize()
        await s.fill("Title", "Body")
        s.scope = SubmissionScope(base, s.recorder.capture)
        s.read_only = False
        await s.close()
        assert s.closed and s.read_only and s.scope is None and s.login_scope is None
        assert not server.writes


async def test_request_can_be_registered_before_context_event_without_duplication(tmp_path):
    from poster.recorder.capture import Recorder
    from types import SimpleNamespace

    class Context:
        handlers = {}

        async def expose_binding(self, *args):
            pass

        async def add_init_script(self, *args):
            pass

        def on(self, event, handler):
            self.handlers[event] = handler

    class Request:
        resource_type = "fetch"
        redirected_from = None
        post_data_buffer = b"subject=Title&message=Body"
        headers = {"content-type": "application/x-www-form-urlencoded"}
        frame = SimpleNamespace(url="https://example.test/form")
        method = "POST"
        url = "https://example.test/send"

    recorder = Recorder("https://example.test/form", tmp_path)
    context = Context()
    await recorder.attach(context)
    request = Request()
    recorder.record_request(request)
    recorder.request_records[request]["blocked"] = True
    context.handlers["request"](request)
    assert len(recorder.capture.requests) == 1 and recorder.capture.requests[0]["blocked"]


async def test_cancel_after_real_post_keeps_unknown_and_never_repeats(tmp_path):
    from poster.benchmark import corpus
    from poster.fixtures_benchmark import serve_benchmark

    case = {**corpus()[0], "id": "cancel-proof", "negative": "slow", "response_delay": 1.2}
    with serve_benchmark([case]) as (server, base):
        s = RecognitionSession(base + "/bench/cancel-proof", tmp_path / "profile", headless=True)
        try:
            await s.start()
            await s.recognize()
            task = asyncio.create_task(
                s.test_publish("Cancel title", "Cancel body", "cancel", confirmed=True)
            )
            for _ in range(200):
                if server.writes:
                    break
                await asyncio.sleep(0.02)
            assert len(server.writes) == 1
            task.cancel()
            await s.close()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert s.result.state == "UNKNOWN" and s.read_only and s.write_admitted
            with pytest.raises(ValueError, match="ALREADY_ATTEMPTED"):
                await s.test_publish("Again", "Again body", "cancel", confirmed=True)
            await asyncio.sleep(1.3)
            assert len(server.writes) == 1
        finally:
            await s.close()


async def test_extra_required_control_requests_operator_input(tmp_path):
    with serve_readiness() as (server, base):
        s = RecognitionSession(base + "/compose/required", tmp_path / "profile", headless=True)
        try:
            await s.start()
            await s.recognize()
            assert s.result.state == "LEARNING_REQUIRED"
            assert "MISSING_REQUIRED_FIELDS" in s.result.diagnostics
            assert not server.writes
            await s.page.locator("[type=email]").fill("operator@example.test")
            await s.recognize()
            assert s.result.state == "DETECTED"
        finally:
            await s.close()


async def test_pending_background_route_cannot_contaminate_an_admitted_capture(tmp_path):
    from poster.recognition.network import NetworkAnalyzer

    with serve_readiness() as (server, base):
        session = RecognitionSession(base + "/compose/background", tmp_path / "profile", headless=True)
        release, pending = asyncio.Event(), asyncio.Event()
        try:
            await session.start()
            await session.recognize()
            await session.test_publish("Causal title", "Causal body", "pending-route", confirmed=True)
            assert session.result.state == "HTTP_CANDIDATE", session.result.summary()
            assert len(server.writes) == 1

            # Hold a genuine browser request BEFORE the original gate runs.
            # Its context event can already reach the synchronous learner.
            async def paused(route):
                pending.set()
                await release.wait()
                await session.gate(route)

            await session.context.route("**/autosave", paused)
            await session.page.evaluate("void fetch('/autosave',{method:'POST',body:'{}'}).catch(()=>{})")
            await asyncio.wait_for(pending.wait(), 5)
            records = [
                r
                for r in session.recorder.capture.requests
                if r["url"].endswith("/autosave") and r.get("action_id") == session.recorder.capture.action_id
            ]
            assert records and all(r.get("blocked") is True for r in records)
            analysis = NetworkAnalyzer().analyze(session.recorder.capture)
            assert analysis.linked and "UNLINKED_REQUESTS" not in analysis.diagnostics
            assert len(server.writes) == 1
        finally:
            release.set()
            await session.close()
