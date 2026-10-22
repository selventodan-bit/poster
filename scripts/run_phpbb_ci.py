"""Bootstrap, test and stop a real loopback phpBB; failures fail CI."""

import asyncio
import json
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from check_phpbb import check
from setup_phpbb import setup


def main():
    directory = Path(".poster/phpbb-ci").resolve()
    root = setup(directory)
    with (directory / "server.log").open("w") as log:
        process = subprocess.Popen(
            ["php", "-S", "127.0.0.1:8879"], cwd=root, stdout=log, stderr=subprocess.STDOUT
        )
        try:
            for _ in range(30):
                try:
                    with urlopen("http://127.0.0.1:8879/index.php", timeout=1) as response:
                        if response.status == 200:
                            break
                except URLError:
                    time.sleep(0.2)
            else:
                raise RuntimeError("phpBB did not start")
            report = asyncio.run(
                check("http://127.0.0.1:8879", directory / "install.yml", directory / "result")
            )
            if report.get("engine") != "phpbb" or report.get("http_status") != "SUCCEEDED":
                raise AssertionError(json.dumps(report))
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    main()
