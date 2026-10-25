"""Fail closed on corrupt/mixed frozen roles; no system Python fallback."""

import hashlib
import json
import sys

import pytest

from poster.dispatcher import launcher
from poster.dispatcher.models import DispatcherError


@pytest.mark.parametrize("damage", ["missing", "corrupt", "manifest", "version"])
def test_frozen_launcher_checks_integrity_and_compiled_version(tmp_path, monkeypatch, damage):
    gui = tmp_path / "PosterDispatcher.exe"
    worker = tmp_path / "PosterWorker.exe"
    gui.touch()
    worker.write_bytes(b"packaged worker")
    info = {
        "commit": "same-build",
        "launchers": {
            "worker": {"file": worker.name, "sha256": hashlib.sha256(worker.read_bytes()).hexdigest()}
        },
    }
    marker = tmp_path / "BUILD_INFO.json"
    marker.write_text(json.dumps(info))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(gui))
    monkeypatch.setenv("POSTER_FROZEN_COMMIT", "same-build")
    assert launcher.command("worker", "--run", "execution") == [str(worker), "--run", "execution"]
    if damage == "missing":
        worker.unlink()
    elif damage == "corrupt":
        worker.write_bytes(b"changed worker")
    elif damage == "manifest":
        marker.write_bytes(b"broken")
    else:
        monkeypatch.setenv("POSTER_FROZEN_COMMIT", "other-build")
    with pytest.raises(DispatcherError) as error:
        launcher.command("worker", "--run", "execution")
    assert error.value.code == (
        "BUILD_INVALID" if damage in {"manifest", "version"} else "WORKER_UNAVAILABLE"
    )


def test_child_environment_has_no_key_or_interpreter_injection(monkeypatch):
    for name in [
        "POSTER_SESSION_KEY",
        "PYTHONHOME",
        "PYTHONPATH",
        "POSTER_PYTHON",
        "_PYI_PARENT_PROCESS_LEVEL",
    ]:
        monkeypatch.setenv(name, "PRIVATE_ENV_CANARY")
    result = launcher.environment()
    assert "PRIVATE_ENV_CANARY" not in result.values()
    assert result["PYTHONUTF8"] == "1"
