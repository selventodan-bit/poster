"""Controlled phpBB installation check. Only loopback targets are permitted.

Prepare phpBB as described in docs/real_engine_testing.md. Credentials stay in an
ignored local install YAML. This script never writes to a public forum.
"""

import argparse
import asyncio
import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from poster.auth import ensure_local_key
from poster.recognition.service import RecognitionSession


async def check(base, install_config, output):
    if urlsplit(base).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Only a controlled loopback installation is allowed")
    output.mkdir(parents=True, exist_ok=True)
    ensure_local_key(output / "private")
    admin = yaml.safe_load(install_config.read_text())["installer"]["admin"]
    unique = uuid.uuid4().hex[:10]
    session = RecognitionSession(
        base + "/ucp.php?mode=login", output / "profile", headless=True, confirmation_timeout=5
    )
    try:
        await session.start()
        await session.recognize()
        assert session.result.auth_required
        await session.authorize_login(confirmed=True)
        await asyncio.sleep(1.1)
        await session.page.locator("#username").fill(admin["name"])
        await session.page.locator("#password").fill(admin["password"])
        await session.page.locator('input[name="login"]').click()
        await session.page.wait_for_timeout(500)
        await session.page.goto(base + "/viewforum.php?f=2")
        result = await session.recognize()
        report = {
            "engine": result.engine,
            "technologies": result.technologies,
            "discovery": str(result.state),
            "navigation": result.navigation,
        }
        if result.state == "DETECTED":
            await asyncio.sleep(1.1)
            await session.test_publish(
                "Poster browser " + unique,
                "Controlled phpBB browser body & Unicode " + unique,
                "phpbb-local",
                confirmed=True,
            )
            report.update(browser_state=str(session.result.state), diagnostics=session.result.diagnostics)
            await session.close()
            if session.result.state == "HTTP_CANDIDATE":
                trial = await session.verify_http(
                    "Poster HTTP " + unique,
                    "Controlled second phpBB HTTP body & Unicode " + unique,
                    confirmed=True,
                )
                report["http_operations"] = trial.operations
                report.update(http_status=trial.status, http_reason=trial.reason)
                if trial.status == "SUCCEEDED":
                    session.save(output / "project")
        (output / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
        return report
    finally:
        await session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8879")
    parser.add_argument("--install-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(".poster/phpbb-check"))
    args = parser.parse_args()
    asyncio.run(check(args.base, args.install_config, args.output))
