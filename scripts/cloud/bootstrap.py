"""Hash-pinned notebook installation. Only committed source, no credentials."""

import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request


def download(url, expected, limit=100_000_000):
    if not url.startswith("https://") or not re.fullmatch("[a-f0-9]{64}", expected):
        raise RuntimeError("BOOTSTRAP_INTEGRITY")
    with urllib.request.urlopen(url, timeout=60) as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit or hashlib.sha256(raw).hexdigest() != expected:
        raise RuntimeError("BOOTSTRAP_INTEGRITY")
    return raw


def install(commit, source_sha256, requirements_sha256, *, molab=False):
    if not re.fullmatch("[a-f0-9]{40}", commit) or sys.version_info < (3, 11):
        raise RuntimeError("BOOTSTRAP_RUNTIME")
    with tempfile.TemporaryDirectory(prefix="poster-install-") as temp:
        root = Path(temp)
        base = "https://raw.githubusercontent.com/selventodan-bit/poster/" + commit + "/"
        requirements = root / "requirements.txt"
        requirements.write_bytes(
            download(base + "requirements-cloud-runtime.txt", requirements_sha256, 2_000_000)
        )
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--require-hashes", "-r", str(requirements)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        archive = root / "poster.tar.gz"
        archive.write_bytes(
            download("https://codeload.github.com/selventodan-bit/poster/tar.gz/" + commit, source_sha256)
        )
        with tarfile.open(archive) as stream:
            stream.extractall(root / "source", filter="data")
        source = next((root / "source").iterdir())
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-deps", "--no-build-isolation", str(source)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        if molab:
            # Molab's native remote storage library is not yet on PyPI. Pin its
            # complete upstream source, dependencies above, and archive hash.
            sha = "dd9bb3025b26091d4a29ac4e81cc3d88910203bd"
            archive = root / "gdrive.tar.gz"
            archive.write_bytes(
                download(
                    "https://codeload.github.com/marimo-team/gdrive-fsspec/tar.gz/" + sha,
                    "ef1cdacf6fbfeaf5bb097361487114c9b872733e32fa6511603581f7e23c5408",
                    5_000_000,
                )
            )
            with tarfile.open(archive) as stream:
                stream.extractall(root / "gdrive", filter="data")
            source = next((root / "gdrive").iterdir())
            env = dict(os.environ, SETUPTOOLS_SCM_PRETEND_VERSION_FOR_GDRIVE_FSSPEC="0.0.0+dd9bb3025")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-deps", "--no-build-isolation", str(source)],
                check=True,
                stdout=subprocess.DEVNULL,
                env=env,
            )
    os.environ["POSTER_CLOUD_SOURCE_COMMIT"] = commit
    return commit
