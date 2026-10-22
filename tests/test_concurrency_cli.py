import asyncio
import json
import subprocess
import sys
from collections import defaultdict

import httpx

from poster.engine import Engine
from poster.models import Site
from poster.persistence import State
from poster.project import Project
from poster.runtime import Runtime
from poster.storage.local import Registry
from test_project_queue import publish


async def test_global_and_actual_destination_host_limits(tmp_path, make_project, monkeypatch):
    active, per_host, maxima = 0, defaultdict(int), defaultdict(int)

    async def handler(req):
        nonlocal active
        active += 1
        per_host[req.url.host] += 1
        maxima["global"] = max(maxima["global"], active)
        maxima[req.url.host] = max(maxima[req.url.host], per_host[req.url.host])
        try:
            await asyncio.sleep(0.01)
            return httpx.Response(200, text="confirmed")
        finally:
            active -= 1
            per_host[req.url.host] -= 1

    transport = httpx.MockTransport(handler)

    def runtime_factory(project, **kwargs):
        return Runtime(project, transport=transport, **kwargs)

    monkeypatch.setattr("poster.engine.Runtime", runtime_factory)
    root, state = tmp_path / "projects", State(tmp_path / "db.sqlite")
    for index in range(3):
        base = f"https://host{index % 2}.test"
        steps = [
            {"id": "request", "op": "request", "url": "/", "save_as": "r"},
            {"id": "confirm", "op": "verify", "source": "r", "kind": "contains", "expected": "confirmed"},
        ]
        original = make_project(base, steps=steps)
        site = Site.model_validate(
            {**original.site.model_dump(), "id": f"p{index}", "schedule": {"max_concurrent": 5}}
        )
        p = Project(site, original.workflow, str(index))
        publish(p, root / p.site.id)
        for j in range(4):
            state.enqueue(p, {}, f"{index}-{j}")
    await Engine(Registry(root), state, max_concurrent_http=2, max_per_host=1).run(asyncio.Event(), once=True)
    assert len(state.rows()) == 12
    assert all(r["status"] == "SUCCEEDED" for r in state.rows())
    assert maxima["global"] == 2
    assert maxima["host0.test"] == maxima["host1.test"] == 1
    state.close()


async def test_engine_in_separate_process_has_no_browser(tmp_path, make_project, fixture_site):
    server, base = fixture_site
    root = tmp_path / "projects"
    publish(make_project(base, csrf=True), root / "csrf")
    state = State(tmp_path / "state.sqlite")
    state.enqueue(make_project(base, csrf=True), {"title": "CLI only", "body": "Without Chromium"}, "cli")
    state.close()
    # Import hook makes a browser import fail in the separate executor process.
    script = """import sys, importlib.abc
class NoBrowser(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname.startswith('playwright'):
   raise RuntimeError('Browser dependency imported in HTTP executor')
sys.meta_path.insert(0, NoBrowser())
from poster.cli import main
raise SystemExit(main(sys.argv[1:]))
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        "engine",
        "--projects",
        str(root),
        "--db",
        str(tmp_path / "state.sqlite"),
        "--once",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
    assert process.returncode == 0, (stdout, stderr)
    state = State(tmp_path / "state.sqlite")
    assert state.rows()[0]["status"] == "SUCCEEDED"
    assert len(server.posts) == 1
    state.close()


def test_cli_dry_run_exit_code_and_validation(tmp_path, make_project):
    project = tmp_path / "classic"
    publish(make_project("https://safe.test"), project)
    command = [sys.executable, "-m", "poster"]
    r = subprocess.run(
        command + ["replay", str(project), "--title", "A", "--body", "B", "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0
    assert json.loads(r.stdout)["status"] == "REVIEW_REQUIRED"
    r = subprocess.run(
        command + ["validate", str(tmp_path / "missing")], capture_output=True, text=True, check=False
    )
    assert r.returncode == 1
