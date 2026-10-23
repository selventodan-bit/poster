"""Dependency-free launcher support. Never touches .poster user data."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]


def run(command):
    subprocess.run(command, cwd=ROOT, check=True)


def environment_healthy(python):
    # -I excludes the working directory and PYTHONPATH. Otherwise source
    # imports + leftover egg-info can disguise an uninstalled editable package.
    probe = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            'import poster.recorder.app,playwright;from importlib.metadata import version;assert version("poster-http")=="0.2.0"',
        ],
        cwd=ROOT,
        capture_output=True,
    )
    return (
        probe.returncode == 0
        and subprocess.run([str(python), "-m", "pip", "check"], cwd=ROOT, capture_output=True).returncode == 0
    )


def bootstrap(check_only=False, *, accept_dependencies=False, install_chromium=False):
    if sys.version_info[:2] not in {(3, 11), (3, 12), (3, 13)}:
        raise ValueError("Install Python 3.11, 3.12 or 3.13 from python.org (include Tcl/Tk and pip).")
    env = ROOT / ".venv"
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.exists():
        try:
            probe = subprocess.run(
                [
                    str(python),
                    "-c",
                    "import json,sys;import pip;print(json.dumps(list(sys.version_info[:2])))",
                ],
                capture_output=True,
                text=True,
            )
            compatible = probe.returncode == 0 and json.loads(probe.stdout) == list(sys.version_info[:2])
        except (OSError, ValueError):
            compatible = False
        if not compatible:
            env.rename(ROOT / (".venv-backup-" + uuid.uuid4().hex[:8]))
    if not python.exists():
        if not accept_dependencies:
            raise ValueError(
                "Python dependencies need installation. Use the portable Poster.exe package, or explicitly pass --accept-dependencies."
            )
        print("Creating Python environment...", flush=True)
        run([sys.executable, "-m", "venv", str(env)])
    stamp = env / "poster-install.sha256"
    fingerprint = hashlib.sha256(
        (ROOT / "pyproject.toml").read_bytes()
        + (ROOT / "requirements-recorder.txt").read_bytes()
        + str(ROOT).encode()
    ).hexdigest()
    healthy = environment_healthy(python)
    if not healthy or not stamp.exists() or stamp.read_text() != fingerprint:
        if not accept_dependencies:
            raise ValueError("Dependency repair requires consent. Use --accept-dependencies.")
        print("Installing current Poster and Recorder dependencies...", flush=True)
        run([str(python), "-m", "pip", "install", "-e", ".[recorder]", "-c", "requirements-recorder.txt"])
        run([str(python), "-m", "pip", "check"])
        stamp.write_text(fingerprint)
    browser_probe = """from playwright.sync_api import sync_playwright
from poster.recorder.capture import chromium_options
with sync_playwright() as p:
    b=p.chromium.launch(**chromium_options(True,p.chromium.executable_path));b.close()
"""
    if subprocess.run([str(python), "-c", browser_probe], capture_output=True).returncode:
        if os.environ.get("POSTER_CHROMIUM"):
            raise ValueError(
                "POSTER_CHROMIUM points to an unusable browser. Correct or remove this variable."
            )
        if install_chromium:
            print("Installing Chromium with explicit consent...", flush=True)
            run([str(python), "-m", "playwright", "install", "chromium"])
            run([str(python), "-c", browser_probe])
        elif check_only:
            raise ValueError(
                "Chrome/Chromium is missing. Choose an installed browser in the app, or explicitly pass --install-browser."
            )
    print("Poster environment and Chromium verified.", flush=True)
    if not check_only:
        run([str(python), "-m", "poster", "recorder"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--accept-dependencies", action="store_true")
    parser.add_argument("--install-browser", action="store_true")
    args = parser.parse_args()
    try:
        bootstrap(
            args.check, accept_dependencies=args.accept_dependencies, install_chromium=args.install_browser
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(
            "Recorder startup failed: "
            + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
            + ".",
            file=sys.stderr,
        )
        print(
            "Check Python, internet access and write permission to the extracted folder. See WINDOWS_README.txt.",
            file=sys.stderr,
        )
        sys.exit(1)
