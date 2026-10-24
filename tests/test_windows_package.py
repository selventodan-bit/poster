import importlib.util
import json
import os
import subprocess
import sys
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
    launcher.bootstrap(True, accept_dependencies=True)
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
        launcher.bootstrap(True, accept_dependencies=True)
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


def test_source_imports_and_egg_info_cannot_disguise_an_uninstalled_package(tmp_path, monkeypatch):
    launcher = module("windows_bootstrap")
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    env = tmp_path / "clean env"
    subprocess.run([sys.executable, "-m", "venv", str(env)], check=True)
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    for name in [
        "poster/__init__.py",
        "poster/recorder/__init__.py",
        "poster/recorder/app.py",
        "playwright.py",
    ]:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    metadata = tmp_path / "poster_http.egg-info"
    metadata.mkdir()
    (metadata / "PKG-INFO").write_text("Metadata-Version: 2.1\nName: poster-http\nVersion: 0.2.0\n")
    probe = 'import poster.recorder.app,playwright;from importlib.metadata import version;assert version("poster-http")=="0.2.0"'
    assert subprocess.run([str(python), "-c", probe], cwd=tmp_path).returncode == 0
    assert not launcher.environment_healthy(python)


def test_source_launcher_requires_dependency_consent_before_mutation(tmp_path, monkeypatch):
    launcher = module("windows_bootstrap")
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="--accept-dependencies"):
        launcher.bootstrap(True)
    assert not (tmp_path / ".venv").exists()


def test_missing_browser_is_never_implicitly_downloaded(tmp_path, monkeypatch):
    launcher = module("windows_bootstrap")
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.delenv("POSTER_CHROMIUM", raising=False)
    (tmp_path / "requirements-recorder.txt").write_text("")
    (tmp_path / "pyproject.toml").write_text("[project]")
    python = tmp_path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_text("existing")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if "import json,sys;" in args[-1]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(list(sys.version_info[:2])))
        return SimpleNamespace(returncode=int("with sync_playwright()" in args[-1]))

    monkeypatch.setattr(launcher.subprocess, "run", run)
    with pytest.raises(ValueError, match="--install-browser"):
        launcher.bootstrap(True, accept_dependencies=True)
    assert not any("playwright" in call and "install" in call for call in calls)


def test_executable_bundle_identifies_commit_and_excludes_private_state(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "Poster.exe").write_bytes(b"test executable")
    builder = module("build_windows_app")
    checksum = builder.package(bundle, tmp_path / "out", "a" * 40)
    with zipfile.ZipFile(tmp_path / "out/poster-windows.zip") as archive:
        assert {"Poster.exe", "BUILD_INFO.json", "WINDOWS_README.txt"} <= set(archive.namelist())
        assert json.loads(archive.read("BUILD_INFO.json")) == {"commit": "a" * 40, "browser_bundled": False}
        assert checksum in (tmp_path / "out/poster-windows.sha256").read_text()
    (bundle / ".poster").mkdir()
    (bundle / ".poster/session.key").write_text("private")
    with pytest.raises(ValueError, match="Private"):
        builder.package(bundle, tmp_path / "bad", "b" * 40)


def test_full_maker_package_has_distinct_identity_checksum_and_measured_sizes(tmp_path):
    bundle = tmp_path / "full bundle"
    bundle.mkdir()
    (bundle / "Poster.exe").write_bytes(b"windowed exe")
    (bundle / "QtWebEngineProcess.exe").write_bytes(b"Qt subprocess")
    builder = module("build_windows_app")
    output = tmp_path / "out"
    checksum = builder.package(bundle, output, "c" * 40, embedded=True)
    with zipfile.ZipFile(output / "poster-maker-windows.zip") as archive:
        info = json.loads(archive.read("BUILD_INFO.json"))
        assert info == {"commit": "c" * 40, "browser_bundled": True, "browser_provider": "qt"}
        report = json.loads((output / "build-report.json").read_text())
        assert report["unpacked_bytes"] == sum(item.file_size for item in archive.infolist())
        assert report["zip_bytes"] == (output / "poster-maker-windows.zip").stat().st_size
        assert report["sha256"] == checksum
    assert checksum in (output / "poster-maker-windows.sha256").read_text()
    assert not (output / "poster-windows.zip").exists()
