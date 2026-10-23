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


def bootstrap(check_only=False):
    if sys.version_info[:2] not in {(3, 11), (3, 12), (3, 13)}:
        raise ValueError("Install Python 3.11, 3.12 or 3.13 from python.org (include Tcl/Tk and pip).")
    env = ROOT / ".venv"
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.exists():
        probe = subprocess.run(
            [str(python), "-c", "import json,sys;print(json.dumps(list(sys.version_info[:2])))"],
            capture_output=True,
            text=True,
        )
        try:
            compatible = probe.returncode == 0 and json.loads(probe.stdout) == list(sys.version_info[:2])
        except ValueError:
            compatible = False
        if not compatible:
            env.rename(ROOT / (".venv-backup-" + uuid.uuid4().hex[:8]))
    if not python.exists():
        print("Creating Python environment...", flush=True)
        run([sys.executable, "-m", "venv", str(env)])
    stamp = env / "poster-install.sha256"
    fingerprint = hashlib.sha256((ROOT / "pyproject.toml").read_bytes() + str(ROOT).encode()).hexdigest()
    healthy = (
        subprocess.run(
            [
                str(python),
                "-c",
                'import poster.recorder.app,playwright;from importlib.metadata import version;assert version("poster-http")=="0.2.0"',
            ],
            capture_output=True,
        ).returncode
        == 0
    )
    if not healthy or not stamp.exists() or stamp.read_text() != fingerprint:
        print("Installing current Poster and Recorder dependencies...", flush=True)
        run([str(python), "-m", "pip", "install", "-e", ".[recorder]"])
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
        print("Installing Chromium...", flush=True)
        run([str(python), "-m", "playwright", "install", "chromium"])
        run([str(python), "-c", browser_probe])
    print("Poster environment and Chromium verified.", flush=True)
    if not check_only:
        run([str(python), "-m", "poster", "recorder"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        bootstrap(args.check)
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
