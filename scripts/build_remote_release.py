"""Pinned hash-verified Linux release. Runtime identities/state are never included."""

import argparse
import hashlib
import json
import platform
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv

ROOT = Path(__file__).resolve().parents[1]


def build(output, *, wheelhouse=False, revision=None):
    output.mkdir(parents=True, exist_ok=True)
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT):
        raise ValueError("Commit code before building a release tied to its SHA")
    if revision and not re.fullmatch("[0-9a-f]{40}", revision):
        raise ValueError("Use a full reviewed Git SHA")
    commit = subprocess.check_output(["git", "rev-parse", revision or "HEAD"], cwd=ROOT, text=True).strip()
    with tempfile.TemporaryDirectory(prefix="poster-linux-release-") as temporary:
        # Build only committed files in a fresh tree; ignored build/lib cannot leak stale modules.
        source = Path(temporary) / "source"
        source.mkdir()
        snapshot = Path(temporary) / "source.tar"
        subprocess.run(
            ["git", "archive", "--format=tar", "--output", str(snapshot), commit], cwd=ROOT, check=True
        )
        with tarfile.open(snapshot) as tar:
            tar.extractall(source, filter="data")
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
            cwd=source,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(root / "wheels")], cwd=source, check=True)
        wheel = next((root / "wheels").glob("*.whl"))
        shutil.copyfile(source / "scripts/install_remote.py", root / "install_remote.py")
        # Optional assets belong to the archived revision, never this checkout.
        # Upgrade tests also build releases from snapshots predating Stage 5D.
        stage5d_doc = source / "docs/POSTER_STAGE5D_EXECUTION_PORTABILITY.md"
        if stage5d_doc.is_file():
            (root / "docs").mkdir()
            shutil.copyfile(stage5d_doc, root / "docs" / stage5d_doc.name)
        stage5d_demo = source / "examples/maker-stage5d"
        if stage5d_demo.is_dir():
            shutil.copytree(stage5d_demo, root / "examples/maker-stage5d")
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
            wheelhouse_python=".".join(map(str, sys.version_info[:2])),
            wheelhouse_platform=platform.system() + "/" + platform.machine(),
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
    parser.add_argument(
        "--revision", help="Full reviewed SHA; useful for a real previous-version upgrade check"
    )
    args = parser.parse_args()
    build(args.output, wheelhouse=args.wheelhouse, revision=args.revision)
