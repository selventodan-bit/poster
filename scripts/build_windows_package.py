"""Build an allowlisted, reproducible source release, never package runtime state."""

import argparse
import hashlib
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build(output):
    output.mkdir(parents=True, exist_ok=True)
    names = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
        )
        .decode()
        .split("\0")
    )
    allowed = {"poster", "scripts", "docs", "examples", "schemas", "tests"}
    top = {
        "README.md",
        "LICENSE",
        "pyproject.toml",
        "uv.lock",
        "requirements-recorder.txt",
        "start_recorder.cmd",
        "WINDOWS_README.txt",
    }
    files = sorted({n for n in names if n and (Path(n).parts[0] in allowed or n in top)})
    target = output / "poster-windows.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in files:
            path = ROOT / name
            assert not path.is_symlink() and path.is_file(), name
            assert not any(
                part in {"node_modules", "__pycache__", ".poster", ".venv"} for part in path.parts
            ), name
            assert not path.name.endswith((".key", ".sqlite", ".enc", ".pyc")), name
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 23, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / "poster-windows.sha256").write_text(checksum + "  " + target.name + "\n", encoding="ascii")
    print("::notice title=Windows ZIP SHA256::" + checksum)
    return target, checksum


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".poster" / "release")
    build(parser.parse_args().output)
