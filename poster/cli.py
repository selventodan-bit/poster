import argparse
import asyncio
import json
import logging
import os
import signal
from dataclasses import asdict
from pathlib import Path

from .content_generator.generator import Generator
from .engine import Engine
from .persistence import State
from .project import load_project, mark_ready
from .runtime import Runtime
from .storage.local import Registry


def parser():
    p = argparse.ArgumentParser(prog="poster", description="Recorder → YAML → HTTP automation")
    sub = p.add_subparsers(dest="command", required=True)
    from .dispatcher.cli import register as register_dispatcher

    register_dispatcher(sub)
    sub.add_parser("keygen", help="Generate a session encryption key; keep it outside project folders")
    sub.add_parser("recorder", help="Open the local Tk Recorder UI")
    sub.add_parser("maker", help="Open the visual ProjectMaker")
    maker_run = sub.add_parser("maker-run", help="Run a versioned Maker graph without the GUI")
    maker_run.add_argument("project", type=Path)
    maker_run.add_argument("--headed", action="store_true")
    maker_run.add_argument("--output", type=Path)
    maker_run.add_argument("--workdir", type=Path, help="Explicit sandbox directory for data files")
    maker_run.add_argument(
        "--allow-external-files", action="store_true", help="Explicitly allow file access outside workdir"
    )
    maker_run.add_argument("--timeout", type=int, default=300, choices=range(1, 3601), metavar="SECONDS")
    maker_run.add_argument(
        "--breakpoints",
        action="store_true",
        help="Return PAUSED at a breakpoint; batch runs ignore them by default",
    )
    maker_run.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="NAME=JSON_OR_TEXT",
        help="Override a declared project input; use secret refs for protected inputs",
    )
    maker_run.add_argument(
        "--secret-env", action="append", default=[], help="Explicitly allow one environment secret name"
    )
    f = sub.add_parser("fixtures", help="Run the two local test sites")
    f.add_argument("--port", type=int, default=8765)
    f2 = sub.add_parser("fixtures-v2", help="Run realistic JS/AJAX and rich-editor fixture sites")
    f2.add_argument("--port", type=int, default=8765)
    recognize = sub.add_parser("recognize", help="Read-only browser survey; never submits a publication")
    recognize.add_argument("url")
    recognize.add_argument("--profile", type=Path, default=Path(".poster/recognition-profile"))
    recognize.add_argument("--headed", action="store_true")
    v2 = sub.add_parser("demo-recognition", help="Real automatic recognition → browser → learned HTTP replay")
    v2.add_argument("--output", type=Path, default=Path(".poster/recognition-demo"))
    knowledge = sub.add_parser("knowledge", help="Manage safe recognition knowledge without a browser")
    knowledge.add_argument("operation", choices=["list", "export", "import", "backup", "disable", "enable"])
    knowledge.add_argument("--db", type=Path, default=Path(".poster/knowledge.sqlite"))
    knowledge.add_argument("--file", type=Path)
    knowledge.add_argument("--rule")
    benchmark = sub.add_parser(
        "benchmark-recognition", help="Controlled held-out browser/HTTP recognition benchmark"
    )
    benchmark.add_argument("--output", type=Path, default=Path(".poster/benchmark"))
    v = sub.add_parser("validate")
    v.add_argument("project", type=Path)
    m = sub.add_parser("ready", help="Write hashes after intentionally editing a complete local tree")
    m.add_argument("directory", type=Path)
    r = sub.add_parser("replay")
    r.add_argument("project", type=Path)
    r.add_argument("--title", required=True)
    r.add_argument("--body", required=True)
    r.add_argument("--target-url", default="")
    r.add_argument("--dry-run", action="store_true")
    g = sub.add_parser("generate")
    g.add_argument("--content", type=Path, default=Path("examples/content"))
    g.add_argument("--campaign", default="italian_info")
    g.add_argument("--seed", type=int, default=1)
    e = sub.add_parser("engine")
    e.add_argument("--projects", type=Path, default=Path(".poster/projects"))
    e.add_argument("--content", type=Path, default=Path("examples/content"))
    e.add_argument("--db", type=Path, default=Path(".poster/state.sqlite"))
    e.add_argument("--poll", type=float, default=60)
    e.add_argument("--max-concurrent-http", type=int, default=10)
    e.add_argument("--max-per-host", type=int, default=1)
    e.add_argument("--once", action="store_true")
    e.add_argument("--drive-root", help="Drive AUTOMATOR folder ID; downloaded into --projects parent")
    q = sub.add_parser("enqueue")
    q.add_argument("project", type=Path)
    q.add_argument("--db", type=Path, default=Path(".poster/state.sqlite"))
    q.add_argument(
        "--key", required=True, help="Stable unique logical action key; reused key does not resend"
    )
    q.add_argument("--title", required=True)
    q.add_argument("--body", required=True)
    q.add_argument("--target-url", default="")
    for name in ("report", "reconcile"):
        c = sub.add_parser(name)
        c.add_argument("--db", type=Path, default=Path(".poster/state.sqlite"))
    for name in ("backup", "restore"):
        c = sub.add_parser(name)
        c.add_argument("snapshot", type=Path)
        c.add_argument("--db", type=Path, default=Path(".poster/state.sqlite"))
    for name in ("drive-backup", "drive-restore"):
        c = sub.add_parser(name)
        c.add_argument("snapshot", type=Path)
        c.add_argument("--folder-id" if name == "drive-backup" else "--file-id", required=True)
    d = sub.add_parser(
        "demo", help="Real browser capture, hot reload, generated content, HTTP replay and restart"
    )
    d.add_argument("--output", type=Path, default=Path("demo-output"))
    return p


async def run_engine(a):
    state = State(a.db)
    drive = None
    content = a.content
    if a.drive_root:
        from .storage.drive import Drive, GoogleToken

        if a.projects.name != "projects":
            raise ValueError("Drive cache path must end in /projects")
        drive = Drive(a.drive_root, a.projects.parent, GoogleToken(os.getenv("POSTER_GOOGLE_CREDENTIALS")))
        content = a.projects.parent / "content"
    engine = Engine(
        Registry(a.projects),
        state,
        content_root=content,
        drive=drive,
        max_concurrent_http=a.max_concurrent_http,
        max_per_host=a.max_per_host,
        poll_interval=a.poll,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, ValueError):
            pass  # Windows: asyncio.run handles KeyboardInterrupt and persists recovery state.
    try:
        await engine.run(stop, once=a.once)
    finally:
        if drive:
            await drive.close()
        state.close()


def main(argv=None):
    a = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        if a.command == "dispatcher":
            from .dispatcher.cli import main as dispatcher_main

            return dispatcher_main(a)
        elif a.command == "keygen":
            from cryptography.fernet import Fernet

            print(Fernet.generate_key().decode())
        elif a.command == "fixtures":
            from .fixtures import serve_forever

            serve_forever(port=a.port)
        elif a.command in {"recorder", "maker"}:
            from .recorder.app import launch

            launch(initial_tab="maker" if a.command == "maker" else "recorder")
        elif a.command == "maker-run":
            from .maker.storage import load_graph
            from .maker.runtime import GraphRunner

            async def execute_graph():
                graph, vault = load_graph(a.project)
                vault.env_allowed.update(a.secret_env)
                inputs = {}
                for item in a.input:
                    name, separator, value = item.partition("=")
                    if not separator:
                        raise ValueError("INPUT_INVALID")
                    try:
                        value = json.loads(value)
                    except ValueError:
                        pass
                    if (
                        name in graph.variables
                        and graph.variables[name].secret
                        and not (isinstance(value, dict) and set(value) == {"secret_ref"})
                    ):
                        raise ValueError("INPUT_SECRET_REF_REQUIRED")
                    inputs[name] = value
                runner = GraphRunner(
                    graph,
                    vault=vault,
                    headless=not a.headed,
                    inputs=inputs,
                    workdir=a.workdir,
                    allow_external_files=a.allow_external_files,
                    project_path=a.project,
                    max_seconds=a.timeout,
                )
                try:
                    status = await runner.run(breakpoints=a.breakpoints)
                    return {
                        "status": status,
                        "results": [asdict(r) for r in runner.results],
                        "variables": runner.context.variables.snapshot(),
                        "workdir": str(runner.context.files.root),
                    }
                finally:
                    await runner.close()

            report = asyncio.run(execute_graph())
            if a.output:
                a.output.parent.mkdir(parents=True, exist_ok=True)
                a.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False))
            return 0 if report["status"] == "SUCCEEDED" else 1
        elif a.command == "fixtures-v2":
            from .fixtures_recognition import serve_forever

            serve_forever(a.port)
        elif a.command == "recognize":
            from .recognition.service import RecognitionSession

            async def survey():
                session = RecognitionSession(a.url, a.profile, headless=not a.headed)
                try:
                    await session.start()
                    return (await session.recognize()).summary()
                finally:
                    await session.close()

            print(json.dumps(asyncio.run(survey()), ensure_ascii=False, indent=2))
        elif a.command == "demo-recognition":
            from .demo_recognition import demo

            print(json.dumps(asyncio.run(demo(a.output)), ensure_ascii=False, indent=2))
        elif a.command == "knowledge":
            from .recognition.knowledge import KnowledgeBase

            kb = KnowledgeBase(a.db)
            try:
                if a.operation == "list":
                    print(
                        json.dumps(
                            [
                                {
                                    "id": r.id,
                                    "maturity": r.maturity,
                                    "enabled": r.enabled,
                                    "successes": r.http_successes,
                                    "failures": r.failures,
                                }
                                for r in kb.rules()
                            ],
                            indent=2,
                        )
                    )
                elif a.operation in {"disable", "enable"}:
                    if not a.rule:
                        raise ValueError("--rule is required")
                    kb.disable(a.rule, a.operation == "disable")
                else:
                    if not a.file:
                        raise ValueError("--file is required")
                    {"export": kb.export_rules, "import": kb.import_rules, "backup": kb.backup}[a.operation](
                        a.file
                    )
            finally:
                kb.close()
        elif a.command == "benchmark-recognition":
            from .benchmark import benchmark

            print(json.dumps(asyncio.run(benchmark(a.output)), ensure_ascii=False, indent=2))
        elif a.command == "validate":
            project = load_project(a.project)
            print(
                json.dumps(
                    {"id": project.site.id, "revision": project.revision, "status": project.site.status},
                    indent=2,
                )
            )
        elif a.command == "ready":
            mark_ready(a.directory)
            print("READY manifest written last")
        elif a.command == "replay":
            result = asyncio.run(
                Runtime(load_project(a.project)).run(
                    {"title": a.title, "body": a.body, "target_url": a.target_url}, dry_run=a.dry_run
                )
            )
            print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
            return (
                0 if result.status == "SUCCEEDED" or (a.dry_run and result.status == "REVIEW_REQUIRED") else 1
            )
        elif a.command == "generate":
            print(
                json.dumps(
                    asdict(Generator(a.content).generate(a.campaign, a.seed)), ensure_ascii=False, indent=2
                )
            )
        elif a.command == "engine":
            asyncio.run(run_engine(a))
        elif a.command == "enqueue":
            state = State(a.db)
            try:
                print(
                    state.enqueue(
                        load_project(a.project),
                        {"title": a.title, "body": a.body, "target_url": a.target_url},
                        a.key,
                    )
                )
            finally:
                state.close()
        elif a.command == "report":
            state = State(a.db)
            try:
                print(json.dumps(state.rows(), ensure_ascii=False, indent=2))
            finally:
                state.close()
        elif a.command == "reconcile":
            state = State(a.db)
            try:
                asyncio.run(Engine(Registry(Path(".poster/projects")), state).reconcile())
                print(json.dumps(state.rows(), ensure_ascii=False, indent=2))
            finally:
                state.close()
        elif a.command == "backup":
            state = State(a.db)
            try:
                state.backup(a.snapshot)
            finally:
                state.close()
            print("Consistent SQLite snapshot created")
        elif a.command == "restore":
            State.restore(a.snapshot, a.db)
            print("Snapshot restored; next Engine start will reconcile interrupted state")
        elif a.command in {"drive-backup", "drive-restore"}:
            from .storage.drive import Drive, GoogleToken

            async def transfer():
                drive = Drive(
                    "unused", Path(".poster/drive"), GoogleToken(os.getenv("POSTER_GOOGLE_CREDENTIALS"))
                )
                try:
                    if a.command == "drive-backup":
                        print(await drive.upload_backup(a.folder_id, a.snapshot))
                    else:
                        await drive.download_backup(a.file_id, a.snapshot)
                        print("Downloaded. Run poster restore to validate and restore into a new DB.")
                finally:
                    await drive.close()

            asyncio.run(transfer())
        elif a.command == "demo":
            from .demo import demo

            print(json.dumps(asyncio.run(demo(a.output)), ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Exceptions from external servers / authentication may contain secret-bearing URLs.
        print(f"Operation failed ({type(exc).__name__}). See docs/troubleshooting.md; no secrets logged.")
        return 1
    return 0
