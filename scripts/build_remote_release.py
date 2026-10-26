"""Pinned hash-verified Linux release. Runtime identities/state are never included."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv

ROOT = Path(__file__).resolve().parents[1]


def build(output, *, wheelhouse=False):
    output.mkdir(parents=True, exist_ok=True)
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT):
        raise ValueError("Commit code before building a release tied to its SHA")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    with tempfile.TemporaryDirectory(prefix="poster-linux-release-") as temporary:
        root = Path(temporary) / "poster-linux"
        root.mkdir()
        subprocess.run(
            [
                "uv",
                "export",
                "--frozen",
                "--no-dev",
                "--extra",
                "recorder",
                "--no-emit-project",
                "--output-file",
                str(root / "requirements-remote.txt"),
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(root / "wheels")], cwd=ROOT, check=True)
        wheel = next((root / "wheels").glob("*.whl"))
        shutil.copyfile(ROOT / "scripts/install_remote.py", root / "install_remote.py")
        if wheelhouse:
            download_env = Path(temporary) / "download-env"
            venv.EnvBuilder(with_pip=True).create(download_env)
            subprocess.run(
                [
                    str(download_env / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")),
                    "-m",
                    "pip",
                    "download",
                    "--only-binary=:all:",
                    "--require-hashes",
                    "-r",
                    str(root / "requirements-remote.txt"),
                    "--dest",
                    str(root / "wheelhouse"),
                ],
                cwd=ROOT,
                check=True,
            )
        manifest = dict(
            format="poster.linux-release.v1",
            commit=commit,
            wheel=wheel.relative_to(root).as_posix(),
            files={
                p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob("*")
                if p.is_file()
            },
        )
        (root / "release.json").write_text(json.dumps(manifest, indent=2))
        archive = output / "poster-linux.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(root, arcname="poster-linux")
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        (output / "poster-linux.sha256").write_text(checksum + "  " + archive.name + "\n")
        print(
            "::notice title=Linux release::"
            + json.dumps(dict(commit=commit, sha256=checksum, wheelhouse=wheelhouse))
        )
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".poster/linux-release")
    parser.add_argument("--wheelhouse", action="store_true")
    args = parser.parse_args()
    build(args.output, wheelhouse=args.wheelhouse)
