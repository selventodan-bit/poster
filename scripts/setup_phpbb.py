"""Install a pinned, real phpBB into an empty private test directory. Needs PHP + Composer."""

import argparse
import json
import secrets
import shutil
import subprocess
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile

import yaml

COMMIT = "e40d69c54097dcc4e436f3308c75aa39960ada75"  # upstream release-3.3.15


def setup(directory: Path):
    directory = directory.resolve()
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("Use an empty directory for the controlled installation")
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / "source.zip"
    with urlopen("https://api.github.com/repos/phpbb/phpbb/zipball/" + COMMIT, timeout=60) as response:
        archive.write_bytes(response.read(30_000_000))
    with ZipFile(archive) as z:
        for name in z.namelist():
            rel = Path(name)
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError("Unsafe source archive")
        z.extractall(directory / "source")
    root = next((directory / "source").iterdir()) / "phpBB"
    with (directory / "composer.log").open("w") as log:
        subprocess.run(
            ["composer", "install", "--no-dev", "--prefer-dist", "--no-interaction", "--no-progress"],
            cwd=root,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    config = yaml.safe_load((root / "docs/install-config.sample.yml").read_text())
    config["installer"]["admin"].update(
        # phpBB 3.3 accepts at most 30 characters (20 random bytes produce 27).
        name="admin", password=secrets.token_urlsafe(20), email="local-test@example.invalid"
    )
    config["installer"]["database"]["dbhost"] = str(directory / "phpbb.sqlite3")
    config["installer"]["server"].update(server_name="127.0.0.1", server_port=8879)
    config["installer"]["extensions"] = []
    path = directory / "install.yml"
    path.write_text(yaml.safe_dump(config))
    path.chmod(0o600)
    with (directory / "install.log").open("w") as log:
        subprocess.run(
            ["php", "install/phpbbcli.php", "install", str(path)],
            cwd=root,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    config_file = root / "config.php"
    if not config_file.exists() or "PHPBB_INSTALLED" not in config_file.read_text():
        detail = (
            (directory / "install.log")
            .read_text()[-4000:]
            .replace(config["installer"]["admin"]["password"], "[redacted]")
        )
        raise RuntimeError("phpBB installer did not create an installed configuration: " + detail)
    shutil.move(root / "install", directory / "installer-completed")
    (directory / "installation.json").write_text(
        json.dumps({"commit": COMMIT, "version": "3.3.15", "root": str(root)})
    )
    print(str(root))
    return root


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=Path(".poster/phpbb"))
    setup(parser.parse_args().directory)
