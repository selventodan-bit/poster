"""Safe public cloud deliverables and SHA256; no workspace/state uploads."""

import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]
output = ROOT / "release/cloud"
output.mkdir(parents=True, exist_ok=True)
paths = [
    ROOT / "notebooks",
    ROOT / "examples/maker-stage5c",
    ROOT / "docs/POSTER_STAGE5C_DRIVE_CLOUD_MULTIWORKER.md",
    ROOT / "requirements-cloud-runtime.txt",
    ROOT / "scripts/cloud/bootstrap.py",
]
files = []
for path in paths:
    files.extend(
        p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts
    ) if path.is_dir() else files.append(path)
archive = output / "poster-cloud-launchers.zip"
with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
    for path in sorted(files):
        stream.write(path, path.relative_to(ROOT).as_posix())
report = dict(
    format="poster.cloud.release.v1",
    commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
    archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
    files={p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)},
    live_provider="CLOUD_PROVIDER_NOT_EXECUTED",
)
(output / "poster-cloud-launchers.sha256").write_text(report["archive_sha256"] + "  " + archive.name + "\n")
(output / "release.json").write_text(json.dumps(report, indent=2) + "\n")
print("::notice title=Cloud launcher delivery SHA256::" + json.dumps(report))
