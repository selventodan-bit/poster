"""Build the portable windowed application without downloading any browser."""

import argparse
import hashlib
import json
import importlib.metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def licenses(bundle, embedded):
    shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.txt", bundle / "THIRD_PARTY_NOTICES.txt")
    if embedded:
        shutil.copytree(ROOT / "licenses" / "qt", bundle / "licenses" / "qt", dirs_exist_ok=True)
    for name in (
        "playwright",
        "httpx",
        "pydantic",
        "cryptography",
        "filelock",
        "google-auth",
        "beautifulsoup4",
        "PyYAML",
        "openpyxl",
        "et-xmlfile",
        "defusedxml",
        "psutil",
        "tzdata",
    ):
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        for file in distribution.files or []:
            if any(
                part in {"license", "licenses", "notice", "copying"}
                for part in str(file).lower().replace(".", "/").split("/")
            ):
                source = distribution.locate_file(file)
                if source.is_file() and source.stat().st_size < 5_000_000:
                    relative = Path(*[part for part in Path(file).parts if part not in {"..", ".", "/"}])
                    target = bundle / "licenses" / "packages" / name / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
    for name in ("LICENSE.txt", "LICENSE"):
        source = Path(sys.base_prefix) / name
        if source.is_file():
            target = bundle / "licenses" / "Python.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            break


def package(bundle, output, commit, *, embedded=False):
    output.mkdir(parents=True, exist_ok=True)
    examples = ROOT / "examples" / "maker-stage3b2"
    if examples.is_dir():
        shutil.copytree(examples, bundle / "examples" / "maker-stage3b2", dirs_exist_ok=True)
    info = {"commit": commit, "browser_bundled": embedded}
    launchers = {"maker": "Poster.exe", "service": "PosterService.exe", "worker": "PosterWorker.exe"}
    if embedded:
        launchers["gui"] = "PosterDispatcher.exe"
    info.update(
        modes=list(launchers),
        shared_runtime="_internal",
        launchers={
            role: {"file": name, "sha256": hashlib.sha256((bundle / name).read_bytes()).hexdigest()}
            for role, name in launchers.items()
        },
    )
    if embedded:
        info["browser_provider"] = "qt"
        info["ui"] = "qt"
    (bundle / "BUILD_INFO.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    shutil.copyfile(ROOT / "WINDOWS_README.txt", bundle / "WINDOWS_README.txt")
    (bundle / "docs").mkdir(exist_ok=True)
    for doc in ("POSTER_STAGE5A_REMOTE_LINUX_WORKER.md", "POSTER_STAGE5B_REMOTE_DEPLOYMENT.md"):
        shutil.copyfile(ROOT / "docs" / doc, bundle / "docs" / doc)
    licenses(bundle, embedded)
    name = "poster-maker-windows" if embedded else "poster-windows"
    target = output / (name + ".zip")
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            relative = path.relative_to(bundle)
            if path.is_symlink() or any(p in {".poster", ".venv", "__pycache__"} for p in relative.parts):
                raise ValueError("Private files in executable bundle")
            if path.is_file():
                if path.name.lower() in {"chrome.exe", "headless_shell.exe", "msedge.exe"}:
                    raise ValueError("A browser must not be bundled implicitly")
                archive.write(path, relative)
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / (name + ".sha256")).write_text(checksum + "  " + target.name + "\n", encoding="ascii")
    report = {
        "commit": commit,
        "sha256": checksum,
        "zip_bytes": target.stat().st_size,
        "unpacked_bytes": sum(p.stat().st_size for p in bundle.rglob("*") if p.is_file()),
        "browser_bundled": embedded,
        "launcher_bytes": {name: (bundle / name).stat().st_size for name in launchers.values()},
        "python_runtime_files": [str(p.relative_to(bundle)) for p in bundle.rglob("python3*.dll")],
        "shared_runtime": "_internal (one shared Python runtime and Qt tree; independent OS processes)",
    }
    if embedded and sys.platform == "win32":
        from PySide6.QtCore import qVersion
        from PySide6.QtWebEngineCore import qWebEngineChromiumVersion, qWebEngineChromiumSecurityPatchVersion

        report.update(
            qt=qVersion(),
            chromium=qWebEngineChromiumVersion(),
            chromium_security_patch=qWebEngineChromiumSecurityPatchVersion(),
        )
    (output / "build-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("::notice title=Windows build measurement::" + json.dumps(report))
    print("::notice title=Windows ZIP SHA256::" + checksum)
    return checksum


def main(output, *, embedded=False):
    if sys.platform != "win32":
        raise ValueError("The Windows application must be built on Windows")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    work = ROOT / ".poster" / ("freeze-maker" if embedded else "freeze")
    work.mkdir(parents=True, exist_ok=True)
    hook = work / "rth_build.py"
    hook.write_text(
        "import os\nos.environ['POSTER_FROZEN_COMMIT'] = " + repr(commit) + "\n", encoding="utf-8"
    )
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(work / "dist"),
        "--workpath",
        str(work / "build"),
        str(ROOT / "scripts/windows_dispatcher.spec"),
    ]
    env = {
        **os.environ,
        "POSTER_BUILD_ROOT": str(ROOT),
        "POSTER_BUILD_EMBEDDED": str(int(embedded)),
        "POSTER_BUILD_HOOK": str(hook),
    }
    subprocess.run(command, cwd=ROOT, check=True, env=env)
    package(work / "dist" / "Poster", output, commit, embedded=embedded)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("release"))
    parser.add_argument(
        "--embedded", action="store_true", help="Explicit full Maker build including QtWebEngine"
    )
    args = parser.parse_args()
    main(args.output, embedded=args.embedded)
