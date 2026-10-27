"""Reproducible real MyBB/SMF/Flarum checks, bound exclusively to loopback.

Requires Docker, gh and Python recorder dependencies. Every run gets new source,
database, credentials, browser profile and project. Containers are always removed.
The MyBB B variant uses a documented test plugin with original CSRF verification.
"""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
import traceback
import uuid
from pathlib import Path
from urllib.parse import urljoin
from zipfile import ZipFile

import httpx
from bs4 import BeautifulSoup

from poster.auth import ensure_local_key
from poster.project import load_project
from poster.recognition.knowledge import KnowledgeBase
from poster.recognition.learner import HTTPWorkflowLearner
from poster.recognition.service import RecognitionSession
from poster.runtime import Runtime

SOURCE = {
    "mybb": (
        "1.8.41",
        "mybb/mybb",
        "mybb_1841",
        "48cc76e9e1146ea052756575991f43f51a634f125eea06d05ce9041b03eedeaa",
    ),
    "smf": ("2.1.8", "SimpleMachines/SMF", "8d46c2749c51997d2526e7fa304229ef890316d9", None),
    "flarum": ("2.0.2", "flarum/flarum", "30bc7b40cec6d49c0eb532f3ed531cf375ce4548", None),
}


def command(args, *, log=None, **kwargs):
    return subprocess.run(
        args, check=True, stdout=log if log else subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs
    )


def unpack(archive, target):
    with ZipFile(archive) as z:
        if any(Path(n).is_absolute() or ".." in Path(n).parts for n in z.namelist()):
            raise ValueError("Unsafe upstream archive")
        z.extractall(target)


def wizard(base, kind, settings, output):
    endpoint = "/install/index.php" if kind == "mybb" else "/install.php"
    with httpx.Client(trust_env=False, follow_redirects=True, timeout=60) as client:
        response = client.get(base + endpoint)
        for _ in range(12):
            soup = BeautifulSoup(response.text, "html.parser")
            title = soup.title.get_text() if soup.title else ""
            if kind == "mybb" and "Finish Setup" in title:
                return
            if kind == "smf" and "Good luck!" in soup.get_text():
                return
            form = soup.select_one("form[action]") if kind == "smf" else soup.select_one("form")
            if not form:
                break
            data = {
                n["name"]: n.get("value", "")
                for n in form.select("input[name]")
                if n.get("type") not in {"checkbox", "radio"}
            }
            data.update(
                {k: v for k, v in settings.items() if form.select_one("[name=" + json.dumps(k) + "]")}
            )
            response = client.post(urljoin(str(response.url), form.get("action") or endpoint), data=data)
        # Private output only, never CI annotations: installers can echo passwords.
        (output / "installer-failure.html").write_text(response.text)
        raise RuntimeError("Original installer did not finish; inspect private installer-failure.html")


class Installation:
    def __init__(self, engine, directory, port, *, variant=False):
        self.engine, self.directory, self.port, self.variant = engine, directory.resolve(), port, variant
        self.containers = []
        self.network = None
        self.base = f"http://127.0.0.1:{port}"
        self.password = secrets.token_urlsafe(18)
        self.username = "admin"

    def docker(self, args, **kwargs):
        return command(["docker", *args], **kwargs)

    def exec(self, args, *, workdir="/srv", log=None):
        return command(
            ["docker", "exec", "-e", "COMPOSER_AUTH", "-w", workdir, self.container, *args], log=log
        )

    def start(self):
        directory, engine = self.directory, self.engine
        directory.mkdir(parents=True)
        version, repo, revision, checksum = SOURCE[engine]
        archive = directory / "source.zip"
        if engine == "mybb":
            command(
                [
                    "gh",
                    "release",
                    "download",
                    revision,
                    "--repo",
                    repo,
                    "--pattern",
                    "mybb_1841.zip",
                    "--dir",
                    str(directory),
                ]
            )
            (directory / "mybb_1841.zip").rename(archive)
        else:
            with archive.open("wb") as output:
                command(["gh", "api", f"repos/{repo}/zipball/{revision}"], log=output)
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if checksum and actual != checksum:
            raise ValueError("Upstream archive checksum changed")
        unpack(archive, directory / "source")
        if engine == "mybb":
            self.root = directory / "source/Upload"
        else:
            self.root = next((directory / "source").iterdir())
        suffix = uuid.uuid4().hex[:10]
        self.container = "poster-" + engine + "-" + suffix
        self.network = "poster-cms-" + suffix
        self.docker(["network", "create", self.network])
        db_password = secrets.token_urlsafe(20)
        if engine == "smf":
            database = self.container + "-db"
            self.docker(
                [
                    "run",
                    "-d",
                    "--name",
                    database,
                    "--network",
                    self.network,
                    "-e",
                    "MARIADB_ROOT_PASSWORD",
                    "mariadb:11.4",
                ],
                env={**os.environ, "MARIADB_ROOT_PASSWORD": db_password},
            )
            self.containers.append(database)
            for _ in range(60):
                ready = subprocess.run(
                    ["docker", "exec", database, "healthcheck.sh", "--connect", "--innodb_initialized"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if ready.returncode == 0:
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("MariaDB did not initialize")
            for name in (
                "install.php",
                "install_2-1_mysql.sql",
                "install_2-1_postgresql.sql",
                "Settings.php",
                "Settings_bak.php",
            ):
                candidate = self.root / "other" / name
                if candidate.exists():
                    shutil.copy(candidate, self.root / name)
        self.docker(
            [
                "run",
                "-d",
                "--name",
                self.container,
                "--network",
                self.network,
                "-p",
                f"127.0.0.1:{self.port}:{self.port}",
                "-e",
                "PHP_CLI_SERVER_WORKERS=4",
                "-v",
                str(self.root) + ":/srv",
                "-v",
                "/etc/ssl/certs:/etc/ssl/certs:ro",
                "poster-cms-php",
                "sleep",
                "infinity",
            ]
        )
        self.containers.append(self.container)
        if engine == "flarum":
            for name in ("composer.json", "composer.lock"):
                shutil.copy(Path("scripts/integration/flarum-" + name), self.root / name)
            command(
                [
                    "gh",
                    "release",
                    "download",
                    "--repo",
                    "composer/composer",
                    "--pattern",
                    "composer.phar",
                    "--dir",
                    str(self.root),
                ]
            )
            with (directory / "composer.log").open("w") as log:
                self.exec(
                    [
                        "php",
                        "composer.phar",
                        "install",
                        "--no-dev",
                        "--prefer-dist",
                        "--no-interaction",
                        "--no-progress",
                    ],
                    log=log,
                )
            (self.root / "forum.sqlite").touch()
            config = {
                "baseUrl": self.base,
                "databaseConfiguration": {"driver": "sqlite", "database": "forum.sqlite"},
                "adminUser": {
                    "username": self.username,
                    "password": self.password,
                    "email": "test@example.invalid",
                },
                "settings": {"forum_title": "Controlled Flarum"},
                "extensions": "flarum-lang-english,flarum-markdown",
            }
            (self.root / "install.json").write_text(json.dumps(config))
            (self.root / "install.json").chmod(0o600)
            with (directory / "install.log").open("w") as log:
                self.exec(["php", "flarum", "install", "--file=install.json", "--no-interaction"], log=log)
            (self.root / "public/router.php").write_text(
                '<?php if(is_file(__DIR__.parse_url($_SERVER["REQUEST_URI"],PHP_URL_PATH)))return false; require __DIR__."/index.php";'
            )
            self.docker(
                [
                    "exec",
                    "-d",
                    "-w",
                    "/srv/public",
                    self.container,
                    "php",
                    "-S",
                    f"0.0.0.0:{self.port}",
                    "router.php",
                ]
            )
        else:
            self.docker(
                [
                    "exec",
                    "-d",
                    self.container,
                    "php",
                    "-d",
                    "default_socket_timeout=5",
                    "-S",
                    f"0.0.0.0:{self.port}",
                ]
            )
            self.wait()
            common = {
                "username": self.username,
                "password1": self.password,
                "password2": self.password,
                "email": "test@example.invalid",
                "server_email": "test@example.invalid",
            }
            if engine == "mybb":
                settings = {
                    "dbengine": "sqlite",
                    "config[sqlite][dbname]": "/srv/forum.sqlite3",
                    "config[sqlite][tableprefix]": "mybb_",
                    "bbname": "Controlled MyBB",
                    "bburl": self.base,
                    "websitename": "Local",
                    "websiteurl": self.base,
                    "cookiedomain": "",
                    "cookiepath": "/",
                    "contactemail": "test@example.invalid",
                    "adminuser": self.username,
                    "adminpass": self.password,
                    "adminpass2": self.password,
                    "adminemail": "test@example.invalid",
                }
            else:
                settings = {
                    **common,
                    "db_type": "mysql",
                    "db_server": database,
                    "db_port": "3306",
                    "db_user": "root",
                    "db_passwd": db_password,
                    "db_name": "forum",
                    "db_prefix": "smf_",
                    "mbname": "Controlled SMF",
                    "boardurl": self.base,
                }
            wizard(self.base, engine, settings, directory)
            # Complete the normal installer cleanup. No CMS code is patched.
            if engine == "smf":
                (self.root / "install.php").unlink()
                # Supported CMS setting: use the original bundled jQuery, so
                # editor initialization does not depend on an external CDN.
                self.docker(
                    [
                        "exec",
                        "-e",
                        "MYSQL_PWD",
                        database,
                        "mariadb",
                        "-uroot",
                        "forum",
                        "-e",
                        "REPLACE INTO smf_settings(variable,value) VALUES('jquery_source','local')",
                    ],
                    env={**os.environ, "MYSQL_PWD": db_password},
                )
            if engine == "mybb" and self.variant:
                shutil.copy(
                    "scripts/integration/mybb/transfer_probe.php",
                    self.root / "inc/plugins/transfer_probe.php",
                )
                # Standard plugin activation cache, same format as the admin UI.
                self.exec(
                    [
                        "php",
                        "-r",
                        'define("IN_MYBB",1);require "global.php";$cache->update("plugins",array("active"=>array("transfer_probe"=>"transfer_probe")));',
                    ]
                )
        self.wait()
        metadata = {
            "engine": engine,
            "version": version,
            "source": f"https://github.com/{repo}",
            "revision": revision,
            "archive_sha256": actual,
            "installation": "controlled loopback Docker",
            "language": "English",
            "variant": "token-source compatibility plugin" if self.variant else "default",
            "database": "MariaDB 11.4" if engine == "smf" else "SQLite",
        }
        if engine == "flarum":
            locked = json.loads((self.root / "composer.lock").read_text())
            core = next(p for p in locked["packages"] if p["name"] == "flarum/core")
            metadata.update(core_source=core["source"]["url"], core_revision=core["source"]["reference"])
        if engine == "smf":
            metadata["jquery_source"] = "original bundled local copy"
        return metadata

    def wait(self):
        for _ in range(40):
            try:
                response = httpx.get(self.base, trust_env=False, timeout=1)
                if response.status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError("Controlled CMS server did not start")

    def stop(self):
        for name in reversed(self.containers):
            subprocess.run(["docker", "rm", "-f", "-v", name], stdout=subprocess.DEVNULL, check=False)
        if self.network:
            subprocess.run(["docker", "network", "rm", self.network], stdout=subprocess.DEVNULL, check=False)


async def publication_window(install):
    if install.engine != "smf":
        return None
    # Pinned SMF Sources/Security.php::spamProtection uses integer time() and
    # DELETE ... WHERE log_time < time()-2 for this installer-created admin.
    # A 2.1-second sleep can leave a gap of exactly 2: the strict comparison
    # retains the row and the next POST is rejected. Observe the real gate.
    query = """require 'Settings.php';
$connection = new mysqli($db_server, $db_user, $db_passwd, $db_name);
$last = (int) $connection->query("SELECT MAX(log_time) FROM " . $db_prefix . "log_floodcontrol WHERE log_type='post'")->fetch_row()[0];
$now = time();
echo json_encode(['ready'=>$last < $now - 2, 'gap'=>$now-$last, 'limit'=>2]);"""
    deadline = time.monotonic() + 10
    observed_closed_at_two = False
    while time.monotonic() < deadline:
        result = await asyncio.to_thread(install.exec, ["php", "-r", query])
        state = json.loads(result.stdout)
        observed_closed_at_two |= state["gap"] == 2 and not state["ready"]
        if state["ready"]:
            return {
                "integer_gap": state["gap"],
                "limit": state["limit"],
                "strict_boundary_at_two_observed": observed_closed_at_two,
            }
        await asyncio.sleep(0.1)
    raise AssertionError("Original SMF publication window did not open")


async def publication(install, directory, kb_path):
    engine, base = install.engine, install.base
    knowledge = KnowledgeBase(kb_path)
    entry = {"mybb": "/member.php?action=login", "smf": "/index.php?action=login", "flarum": "/"}[engine]
    session = RecognitionSession(base + entry, directory / "profile", knowledge=knowledge, headless=True)
    unique = uuid.uuid4().hex[:10]
    try:
        await session.start()
        if engine == "flarum":
            await session.page.get_by_role("button", name="Log In", exact=True).click()
        await session.page.locator('input[type="password"]:visible').wait_for(timeout=15000)
        await session.authorize_login(confirmed=True)
        user, password = {
            "mybb": ("username", "password"),
            "smf": ("user", "passwrd"),
            "flarum": ("identification", "password"),
        }[engine]
        await session.page.locator(f'input[name="{user}"]:visible').fill(install.username)
        await session.page.locator(f'input[name="{password}"]:visible').fill(install.password)
        await (
            session.page.locator("form")
            .filter(has=session.page.locator(f'input[name="{password}"]:visible'))
            .locator('[type="submit"]')
            .click()
        )
        section = {"mybb": "/forumdisplay.php?fid=2", "smf": "/index.php?board=1.0", "flarum": "/"}[engine]
        if engine == "flarum":
            # Flarum reloads the current index after login. A second goto after
            # a fixed 500 ms races that reload and aborts the navigation. Wait
            # for the authenticated header on the real post-login document.
            await session.page.locator("button.Button--user").wait_for(state="visible", timeout=30000)
            await session.page.wait_for_load_state("domcontentloaded")
        else:
            await session.page.wait_for_timeout(500)
            await session.page.goto(base + section)
        await session.recognize()
        (directory / "discovery-private.html").write_text(await session.page.content())
        (directory / "capture-private.json").write_text(
            json.dumps(session.recorder.capture.__dict__, default=str)
        )
        assert session.result.engine == engine and session.result.state == "DETECTED", (
            session.result.summary()
        )
        editor = next(f for f in session.result.forms if f.key == session.result.selected).fields["body"]
        preflight = session.result.transfer.copy()
        await session.test_publish(
            "Browser " + unique,
            "Original CMS Unicode Ж & body " + unique,
            engine + "-controlled",
            confirmed=True,
        )
        assert session.result.state == "HTTP_CANDIDATE", session.result.summary()
        baseline = HTTPWorkflowLearner().learn(session.recorder.capture, "cold-comparison")
        transfer = session.result.transfer.copy()
        if install.variant:
            assert baseline.site["recognition"]["state"] == "LEARNING_REQUIRED", baseline.diagnostics
            assert transfer.get("applied") and transfer.get("bindings", 0) > 0, transfer
            assert preflight.get("writes") == 0 and not preflight.get("http_verified"), preflight
        await session.close()
        # Observe readiness before a single write; never disable or retry flood control.
        flood_windows = [await publication_window(install)]
        trial = await session.verify_http(
            "HTTP " + unique, "Independent HTTP Unicode Ж & body " + unique, confirmed=True
        )
        if trial.status != "SUCCEEDED":
            known = {
                "Read-only recovery could not confirm write": "RECOVERY_UNCONFIRMED",
                "Authentication/authorization rejected (401/403)": "AUTH_REJECTED",
                "Expected confirmation/extraction element is absent": "ELEMENT_ABSENT",
                "Assertion/verification failed": "VERIFY_FAILED",
                "Write returned an error; confirm result before retry": "WRITE_ERROR",
                "Invalid extraction/session data": "EXTRACTION_INVALID",
                "HTTP read transport failure": "READ_TRANSPORT",
                "Write outcome unknown; no automatic resubmission": "WRITE_UNKNOWN",
            }
            print(
                "::error title=Original CMS verification::"
                + json.dumps(
                    {
                        "engine": engine,
                        "status": trial.status,
                        "reason": known.get(trial.reason, "OTHER_SAFE_REASON"),
                        "operations": [
                            {"method": op.get("method"), "status": op.get("status")}
                            for op in trial.operations
                        ],
                    }
                ),
                flush=True,
            )
        assert trial.status == "SUCCEEDED", (trial.status, trial.reason, trial.operations)
        project_path = directory / "project"
        session.save(project_path)
        flood_windows.append(await publication_window(install))
        # Load from disk into the normal HTTP runtime after Recorder has closed.
        replay = await Runtime(load_project(project_path)).run(
            {
                "title": "Replay " + unique,
                "body": "Reused project Unicode Ж & body " + unique,
                "target_url": base,
            }
        )
        assert replay.status == "SUCCEEDED", (replay.status, replay.reason)
        return {
            "engine_detected": session.result.engine,
            "editor": editor.editor,
            "editor_kind": editor.kind,
            "browser": True,
            "chromium_closed_before_http": True,
            "http": trial.status,
            "project_state": session.build.site["recognition"]["state"],
            "reloaded_project": replay.status,
            "preflight": preflight,
            "transfer": transfer,
            "cold_candidate": baseline.site["recognition"]["state"],
            "cold_diagnostics": baseline.diagnostics,
            "http_operations": trial.operations,
            "result_url": replay.result_url,
            "navigation_actions": len(session.result.navigation),
            "publication_windows": flood_windows,
        }
    finally:
        await session.close()
        knowledge.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=list(SOURCE), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18881)
    args = parser.parse_args()
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    ensure_local_key(directory / "private")
    with (directory / "docker-build.log").open("w") as log:
        command(
            ["docker", "build", "-t", "poster-cms-php", "scripts/integration"],
            log=log,
            env={**os.environ, "DOCKER_BUILDKIT": "0"},
        )
    report = []
    for index in range(2 if args.engine == "mybb" else 1):
        target = directory / ("B" if index else "A")
        install = Installation(args.engine, target / "installation", args.port + index, variant=bool(index))
        try:
            metadata = install.start()
            result = asyncio.run(publication(install, target, directory / "knowledge.sqlite"))
            report.append({**metadata, **result})
            (directory / "report.json").write_text(json.dumps(report, indent=2))
            print(json.dumps(report[-1], indent=2), flush=True)
        finally:
            install.stop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        locations = " > ".join(
            f"{Path(f.filename).name}:{f.lineno}:{f.name}"
            for f in traceback.extract_tb(exc.__traceback__)[-4:]
        )
        print(f"::error::Original CMS {type(exc).__name__} at {locations}", flush=True)
        raise
