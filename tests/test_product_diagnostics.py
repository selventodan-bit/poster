"""Diagnostics export uses allowlisted aggregates, never raw configuration or paths."""

import json

from poster.product import diagnostics
from poster.dispatcher.store import Store


def test_diagnostic_report_excludes_account_tokens_paths_and_unknown_statuses(tmp_path, monkeypatch):
    canary = "PRIVATE_DIAGNOSTIC_CANARY"
    roots = {name: tmp_path / (canary + "-" + name) for name in ["maker", "dispatcher", "cloud"]}
    for root in roots.values():
        root.mkdir()
        (root / "config.json").write_text(
            json.dumps(dict(token=canary, url="https://user:" + canary + "@example.org"))
        )
    store = Store(roots["dispatcher"] / "dispatcher.sqlite")
    try:
        report = diagnostics.collect(roots)
    finally:
        store.close()
    text = json.dumps(report)
    assert canary not in text and str(tmp_path) not in text
    assert report["format"] == "poster.diagnostics.v1"
    assert report["external_validation"]["google_drive"] == "CLOUD_PROVIDER_NOT_EXECUTED"
    assert "free_bytes" in report["storage"]["maker"]
    monkeypatch.chdir(tmp_path)
    (tmp_path / "BUILD_INFO.json").write_text(
        json.dumps(dict(commit=canary, version=canary, ui=canary, package=canary, token=canary))
    )
    assert canary not in json.dumps(diagnostics.build_info())
