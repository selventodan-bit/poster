"""Bootstrap, test and stop a real loopback phpBB; failures fail CI."""

import asyncio
import json
import subprocess
import time
from pathlib import Path
import httpx

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
                    with httpx.Client(trust_env=False, timeout=1) as client:
                        response = client.get("http://127.0.0.1:8879/index.php")
                        # phpBB can start a guest session with a normal 302 before
                        # serving the page. Readiness is distinct from publication proof.
                        if response.status_code in {200, 301, 302, 303, 307, 308}:
                            break
                except httpx.HTTPError:
                    pass
                time.sleep(0.2)
            else:
                raise RuntimeError("phpBB did not start: " + (directory / "server.log").read_text()[-4000:])
            report = asyncio.run(
                check("http://127.0.0.1:8879", directory / "install.yml", directory / "result")
            )
            if report.get("engine") != "phpbb" or report.get("http_status") != "SUCCEEDED":
                raise AssertionError(json.dumps(report))
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # This harness uses controlled loopback data and fixed public download URLs.
        # Surface bootstrap failures as a check annotation even when job logs are unavailable.
        print(
            "::error::phpBB integration: " + type(exc).__name__ + ": " + str(exc).replace("\n", "%0A"),
            flush=True,
        )
        raise
