"""Build the portable windowed application without downloading any browser."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def package(bundle, output, commit):
    output.mkdir(parents=True, exist_ok=True)
    (bundle / "BUILD_INFO.json").write_text(
        json.dumps({"commit": commit, "browser_bundled": False}, indent=2), encoding="utf-8"
    )
    shutil.copyfile(ROOT / "WINDOWS_README.txt", bundle / "WINDOWS_README.txt")
    target = output / "poster-windows.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            name = path.relative_to(bundle)
            if path.is_symlink() or any(p in {".poster", ".venv", "__pycache__"} for p in name.parts):
                raise ValueError("Private files in executable bundle")
            if path.is_file():
                if path.name.lower() in {"chrome.exe", "headless_shell.exe", "msedge.exe"}:
                    raise ValueError("A browser must not be bundled implicitly")
                archive.write(path, name)
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / "poster-windows.sha256").write_text(checksum + "  poster-windows.zip\n", encoding="ascii")
    print("::notice title=Windows ZIP SHA256::" + checksum)
    return checksum


def main(output):
    if sys.platform != "win32":
        raise ValueError("The Windows application must be built on Windows")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    work = ROOT / ".poster" / "freeze"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
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
    for distribution in ("playwright", "httpx", "pydantic", "cryptography", "filelock"):
        command += ["--copy-metadata", distribution]
    command.append(str(ROOT / "poster" / "desktop_entry.py"))
    subprocess.run(command, cwd=ROOT, check=True)
    package(work / "dist" / "Poster", output, commit)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("release"))
    main(parser.parse_args().output)
