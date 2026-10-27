"""Readiness never promotes metadata-only checks to runtime verification."""

import pytest

from poster.maker.example_projects import chain
from poster.maker.model import Variable
from poster.maker.profiles import Profiles
from poster.maker.storage import Vault, save_graph
from poster.portability.readiness import inspect
from poster.maker.fixtures import serve_maker


@pytest.mark.parametrize("target", ["windows", "linux", "colab", "molab", "generic"])
async def test_static_matrix_never_claims_actual_runtime(tmp_path, target):
    source = tmp_path / "project.postergraph.json"
    save_graph(source, chain("Portable", [("Files.WriteText", {"path": "out.txt", "text": "ok"})]), Vault())
    report = await inspect(source, target)
    assert (
        report["static_compatible"] and not report["runtime_verified"] and report["status"] == "NOT_VERIFIED"
    )
    assert report["registry_actions"] == 78
    assert report["max_parallel"] == (1 if target in {"colab", "molab"} else 32)


async def test_real_browser_probe_and_missing_executable(tmp_path, monkeypatch):
    with serve_maker() as (_, base):
        source = tmp_path / "project.postergraph.json"
        save_graph(source, chain("Browser", [("Browser.OpenURL", {"url": base + "/login"})]), Vault())
        report = await inspect(source, "generic", actual=True, probe_url=base + "/login")
        assert report["runtime_verified"] and report["observed"]["browser_started"], report
        monkeypatch.setenv("POSTER_CHROMIUM", str(tmp_path / "missing-browser"))
        report = await inspect(source, "generic", actual=True, probe_url=base + "/login")
        assert report["status"] == "BLOCKED" and not report["runtime_verified"]
        assert "BROWSER_UNAVAILABLE" in {i["code"] for i in report["runtime_issues"]}


async def test_missing_secrets_and_public_provider_profile_refused(tmp_path):
    source = tmp_path / "project.postergraph.json"
    graph = chain(
        "Protected", [("Files.WriteText", {"path": "out.txt", "text": {"secret_ref": "sensitive"}})]
    )
    graph.variables["password"] = Variable(secret=True, input=True, required=True)
    save_graph(source, graph, Vault())
    profiles = Profiles(tmp_path / "profiles")
    key = profiles.create("Private")
    profiles.open(key).commit(
        dict(
            cookies=[dict(name="sid", value="PRIVATE_READINESS_CANARY", domain="example.test", path="/")],
            origins=[],
        )
    )
    report = await inspect(source, "colab", actual=True, profile=profiles.open(key), trusted=True)
    assert {"SECRET_REQUIRED", "PROFILE_INCOMPATIBLE"} <= {i["code"] for i in report["static_issues"]}
    assert "PRIVATE_READINESS_CANARY" not in __import__("json").dumps(report)
    assert not report["runtime_verified"]
