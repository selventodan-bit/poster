import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest


def module(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parents[1] / "scripts" / (name + ".py")
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_release_is_reproducible_and_excludes_private_state(tmp_path):
    builder = module("build_windows_package")
    first, checksum = builder.build(tmp_path / "first")
    second, other = builder.build(tmp_path / "second")
    assert checksum == other and first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
        assert {
            "start_recorder.cmd",
            "scripts/windows_bootstrap.py",
            "poster/recorder/app.py",
            "WINDOWS_README.txt",
            "requirements-recorder.txt",
        } <= set(names)
        assert not any(
            any(p in {"node_modules", ".poster", ".venv", "__pycache__"} for p in Path(n).parts)
            for n in names
        )
        assert not any(n.endswith((".key", ".sqlite", ".enc", ".pyc")) for n in names)
        archive.extractall(tmp_path / "Распаковка с пробелами")


def test_launcher_repairs_old_environment_and_preserves_user_data(tmp_path, monkeypatch):
    launcher = module("windows_bootstrap")
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    (tmp_path / "requirements-recorder.txt").write_text("")
    (tmp_path / "pyproject.toml").write_text('[project]\nname="test"')
    python = tmp_path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_text("old")
    private = tmp_path / ".poster" / "private"
    private.mkdir(parents=True)
    (private / "session.key").write_text("preserved key")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if "import json,sys;" in args[-1]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([3, 10]))
        if "venv" in args:
            python.parent.mkdir(parents=True)
            python.write_text("new")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(launcher.subprocess, "run", run)
    launcher.bootstrap(True)
    assert list(tmp_path.glob(".venv-backup-*"))
    assert (private / "session.key").read_text() == "preserved key"
    assert any("pip" in c and "install" in c for c in calls)
    assert not any("recorder" in c for c in calls)


def test_launcher_install_failure_does_not_mark_environment_healthy(tmp_path, monkeypatch):
    launcher = module("windows_bootstrap")
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    (tmp_path / "requirements-recorder.txt").write_text("")
    (tmp_path / "pyproject.toml").write_text("[project]")

    def denied(*args, **kwargs):
        raise PermissionError("No folder access")

    monkeypatch.setattr(launcher.subprocess, "run", denied)
    with pytest.raises(PermissionError):
        launcher.bootstrap(True)
    assert not (tmp_path / ".venv" / "poster-install.sha256").exists()


def test_public_pytest_summary_excludes_traceback_and_secret_values(tmp_path, capsys):
    source = tmp_path / "private.xml"
    source.write_text(
        '<testsuites><testsuite><testcase classname="checks" name="case"><failure>cookie=PRIVATE_TOKEN</failure></testcase></testsuite></testsuites>'
    )
    target = tmp_path / "public.json"
    module("summarize_pytest").summarize(source, target)
    report = json.loads(target.read_text())
    assert report["tests"] == 1 and report["failures"][0]["test"] == "checks.case"
    assert "PRIVATE_TOKEN" not in target.read_text() + capsys.readouterr().out
