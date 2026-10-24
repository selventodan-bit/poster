"""Build the portable windowed application without downloading any browser."""

import argparse
import hashlib
import json
import importlib.metadata
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
    info = {"commit": commit, "browser_bundled": embedded}
    if embedded:
        info["browser_provider"] = "qt"
        info["ui"] = "qt"
    (bundle / "BUILD_INFO.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    shutil.copyfile(ROOT / "WINDOWS_README.txt", bundle / "WINDOWS_README.txt")
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
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--python-option",
        "X utf8",
        "--noupx",
        "--name",
        "Poster",
        "--distpath",
        str(work / "dist"),
        "--workpath",
        str(work / "build"),
        "--specpath",
        str(work),
        "--paths",
        str(ROOT),
        "--collect-all",
        "playwright",
        "--collect-data",
        "poster",
    ]
    if embedded:
        command += ["--hidden-import", "poster.maker.qt_host", "--hidden-import", "poster.maker.qt_app"]
    else:
        command += [
            "--exclude-module",
            "poster.maker.qt_host",
            "--exclude-module",
            "poster.maker.qt_app",
            "--exclude-module",
            "PySide6",
        ]
    for distribution in ("playwright", "httpx", "pydantic", "cryptography", "filelock"):
        command += ["--copy-metadata", distribution]
    command.append(str(ROOT / "poster" / "desktop_entry.py"))
    subprocess.run(command, cwd=ROOT, check=True)
    package(work / "dist" / "Poster", output, commit, embedded=embedded)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("release"))
    parser.add_argument(
        "--embedded", action="store_true", help="Explicit full Maker build including QtWebEngine"
    )
    args = parser.parse_args()
    main(args.output, embedded=args.embedded)
