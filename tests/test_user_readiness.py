import asyncio
from pathlib import Path

import pytest

from poster.fixtures_readiness import serve_readiness
from poster.recognition.service import RecognitionSession
from poster.recognition.policy import ResourcePolicy


@pytest.mark.parametrize("case", ["topic", "reply", "comment", "ajax", "background", "synchronous"])
async def test_independent_shapes_complete_export_restart(case, tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "acceptance", Path(__file__).parents[1] / "scripts/check_windows_e2e.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = await module.check(tmp_path / case, case)
    assert report["writes"] == 3 and report["runtime_new_process"]


@pytest.mark.parametrize("case,detected", [("ambiguous", False), ("unsafe", False), ("delayed", True)])
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
    assert p.safe_url("https://static.example/style.css?v=1")
    assert not p.safe_url("https://static.example/style.css?access%5Ftoken=secret")
    assert not p.safe_url("https://static.example/style.css?%2574oken=secret")
    assert not p.safe_url("https://static.example@127.0.0.1/x")
    with pytest.raises(ValueError):
        ResourcePolicy(["http://127.0.0.1"])

    async def lookup(*args, **kwargs):
        return [(2, 1, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    with pytest.raises(ValueError, match="private"):
        await p.public_addresses("static.example", 443)
