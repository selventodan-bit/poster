"""An executable acceptance check, using genuine browser captures and HTTP writes."""

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from .content_generator.generator import Generator
from .engine import Engine
from .fixtures import serve_fixture
from .persistence import State
from .recorder.builder import analyze, export
from .recorder.capture import Recorder
from .storage.local import Registry


async def until(predicate, timeout=20):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.025)


async def demo(output: Path):
    if output.exists() and any(output.iterdir()):
        raise ValueError("Demo output must be empty; choose a new --output")
    output.mkdir(parents=True, exist_ok=True)
    key = os.environ.get("POSTER_SESSION_KEY") or Fernet.generate_key().decode()
    previous_key = os.environ.get("POSTER_SESSION_KEY")
    os.environ["POSTER_SESSION_KEY"] = key
    private = output / "private"
    private.mkdir(mode=0o700)
    keyfile = private / "session.key"
    keyfile.write_text(key, encoding="ascii")
    keyfile.chmod(0o600)
    examples = Path(__file__).resolve().parent.parent / "examples/content"
    shutil.copytree(examples, output / "content")
    state = State(output / "state.sqlite")
    projects = output / "projects"
    registry = Registry(projects)
    engine = Engine(registry, state, content_root=output / "content", poll_interval=0.05)
    stop = asyncio.Event()
    report = {}
    try:
        with serve_fixture() as (server, base), tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            builds = {}
            for kind in ("classic", "csrf"):
                capture = await Recorder(base + f"/{kind}/new", temp / f"profile-{kind}").automated_fixture()
                build = analyze(capture, kind)
                assert build.site["status"] == "HTTP_READY", build.diagnostics
                build.site["campaign"] = "italian_info"
                builds[kind] = build
            assert len(server.posts) == 2, "Both browser-recorded writes must exist"
            # Recorder contexts are closed. Only httpx executes all subsequent jobs.
            daemon = asyncio.create_task(engine.run(stop))
            try:
                await until(lambda: engine.poll_count >= 1)
                assert not registry.projects
                for kind, build in builds.items():
                    stage = temp / kind
                    export(build, stage)
                    (stage / "targets.csv").write_text(
                        f"id,target_url,seed\nfirst,{base}/{kind}/new,42\n", encoding="utf-8"
                    )
                    from .project import mark_ready

                    mark_ready(stage)
                    shutil.copytree(stage, projects / kind)
                    await until(lambda kind=kind: kind in registry.projects)
                await until(
                    lambda: (
                        len(state.rows()) == 2
                        and all(r["status"] not in {"PENDING", "RUNNING", "LEASED"} for r in state.rows())
                    )
                )
                rows = state.rows()
                assert all(r["status"] == "SUCCEEDED" for r in rows), rows
                expected = Generator(output / "content").generate("italian_info", 42)
                replay_posts = [p for p in server.posts.values() if p["title"] == expected.title]
                assert len(replay_posts) == 2
                assert all(p["body"] == expected.body for p in replay_posts)
                assert len(set(server.token_history)) >= 2, "HTTP must fetch a new CSRF token"
                assert len(server.posts) == 4, "Two browser writes + exactly two HTTP writes"
                report = {
                    "status": "SUCCEEDED",
                    "recorded_sites": ["classic", "csrf"],
                    "browser_writes": 2,
                    "verified_http_writes": len(replay_posts),
                    "csrf_fresh_tokens": len(server.token_history),
                    "hot_load_without_restart": engine.poll_count > 1,
                    "jobs": rows,
                }
            finally:
                stop.set()
                await daemon
            # Same persistent state and targets, restarted engine: no new submissions.
            restarted = Engine(Registry(projects), state, content_root=output / "content")
            await restarted.run(asyncio.Event(), once=True)
            assert len(server.posts) == 4
            report["restart_duplicate_writes"] = 0
            state.backup(output / "snapshot.sqlite")
            report["sqlite_backup"] = "integrity_check: ok"
            report["note"] = (
                "Fixture servers stopped; generated URLs are diagnostic artifacts, not live services"
            )
            (output / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return report
    finally:
        state.close()
        if previous_key is None:
            os.environ.pop("POSTER_SESSION_KEY", None)
        else:
            os.environ["POSTER_SESSION_KEY"] = previous_key
