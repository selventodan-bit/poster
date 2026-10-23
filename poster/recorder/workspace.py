"""Desktop adapters over the existing Recorder, Engine and transactional queue."""

import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import uuid

from ..browser import find_browser
from ..engine import Engine
from ..fixtures_readiness import serve_readiness
from ..persistence import State
from ..project import load_project
from ..recognition.knowledge import KnowledgeBase
from ..recognition.service import RecognitionSession
from ..storage.local import Registry

JOB_TEXT = {
    "PENDING": "В очереди",
    "LEASED": "Подготовка",
    "RUNNING": "Публикуется",
    "SUCCEEDED": "Опубликовано",
    "FAILED": "Ошибка проверки",
    "UNKNOWN": "Проверьте сайт вручную",
    "AUTH_EXPIRED": "Войдите на сайт заново",
    "REVIEW_REQUIRED": "Требуется обучение",
    "CANCELLED": "Удалено из очереди",
}


def linked(path):
    return path.is_symlink() or bool(
        getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
    )


def directory_size(root):
    if not root.exists() or linked(root):
        return 0
    size = 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not linked(Path(folder) / d)]
        for name in files:
            path = Path(folder) / name
            try:
                if not linked(path):
                    size += path.stat().st_size
            except OSError:
                pass
    return size


class Workspace:
    def __init__(self, root=Path(".poster")):
        self.root = Path(root).absolute()
        self.projects = self.root / "projects"
        self.temporary = self.root / "tmp"
        self.db = self.root / "state.sqlite"
        self.active = set()
        self.test_ports = set()
        self.root.mkdir(parents=True, exist_ok=True)
        self.projects.mkdir(exist_ok=True)

    def create_temp(self):
        self.temporary.mkdir(exist_ok=True)
        if linked(self.root) or linked(self.temporary):
            raise ValueError("UNSAFE_TEMP")
        path = Path(tempfile.mkdtemp(prefix="poster-", dir=self.temporary))
        (path / ".poster-owned").write_text("poster-desktop-temp-v1", encoding="ascii")
        self.active.add(path)
        return path

    def clean_temp(self):
        removed = 0
        if linked(self.root) or not self.temporary.exists() or linked(self.temporary):
            return removed
        for path in self.temporary.iterdir():
            if (
                path in self.active
                or not path.name.startswith("poster-")
                or linked(path)
                or not path.is_dir()
            ):
                continue
            marker = path / ".poster-owned"
            try:
                owned = (
                    marker.is_file()
                    and not linked(marker)
                    and marker.read_text(encoding="ascii") == "poster-desktop-temp-v1"
                )
            except (OSError, ValueError):
                owned = False
            if not owned:
                continue
            # Preserve suspicious trees, including junctions, rather than following them.
            if any(
                linked(Path(folder) / name)
                for folder, dirs, files in os.walk(path, followlinks=False)
                for name in dirs + files
            ):
                continue
            size = directory_size(path)
            shutil.rmtree(path)
            removed += size
        return removed

    def projects_list(self):
        result = []
        for path in sorted(self.projects.iterdir()):
            if not path.is_dir() or path.name.startswith(".") or linked(path):
                continue
            try:
                project = load_project(path)
                if project.site.id != path.name:
                    raise ValueError("Project folder must match its id")
                ready = (
                    project.site.status == "HTTP_READY"
                    and project.site.mode == "http"
                    and project.site.enabled
                    and (
                        not project.site.recognition
                        or (
                            project.site.recognition.http_verified
                            and project.site.recognition.state == "HTTP_VERIFIED"
                        )
                    )
                )
                result.append({"id": project.site.id, "path": str(path), "ready": ready})
            except (OSError, ValueError, KeyError):
                result.append({"id": path.name, "path": str(path), "ready": False})
        return result

    def import_project(self, source):
        source = Path(source)
        project = load_project(source)
        destination = self.projects / project.site.id
        if destination.resolve() == source.resolve():
            return project.site.id
        if destination.exists():
            raise ValueError("PROJECT_EXISTS")
        # Copy only the validated immutable manifest; never arbitrary neighboring files.
        manifest = json.loads((source / "READY").read_bytes())
        temporary = self.projects / (".import-" + uuid.uuid4().hex)
        temporary.mkdir()
        try:
            for name in manifest["files"]:
                target = temporary / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((source / name).read_bytes())
            (temporary / "READY").write_bytes((source / "READY").read_bytes())
            load_project(temporary)
            temporary.rename(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return project.site.id

    def system_report(self):
        dependencies = {}
        for name in ("httpx", "pydantic", "cryptography", "playwright", "filelock"):
            try:
                dependencies[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                dependencies[name] = "встроен" if getattr(sys, "frozen", False) else "не найден"
        return {
            "python": sys.version.split()[0],
            "dependencies": dependencies,
            "browser": find_browser(),
            "free_bytes": shutil.disk_usage(self.root).free,
            "data_bytes": directory_size(self.root),
            "temp_bytes": directory_size(self.temporary),
            "projects_bytes": directory_size(self.projects),
        }


class EngineBridge:
    """All methods run on the desktop's asyncio thread, including SQLite access."""

    def __init__(self, workspace):
        self.workspace = workspace
        self.task = self.engine = self.stop_event = self.state = None
        self.error = None

    @property
    def running(self):
        return bool(self.task and not self.task.done())

    async def start(self):
        if self.running:
            return
        registry = Registry(self.workspace.projects)
        registry.poll()
        if not registry.projects:
            raise ValueError("NO_READY_PROJECTS")
        self.error = None
        self.state = State(self.workspace.db)
        self.stop_event = asyncio.Event()
        self.engine = Engine(registry, self.state, poll_interval=0.5)

        async def run():
            try:
                await self.engine.run(self.stop_event)
            except Exception as exc:
                self.error = type(exc).__name__
            finally:
                self.state.close()

        self.task = asyncio.create_task(run())
        await asyncio.sleep(0)

    async def stop(self):
        if self.running:
            self.stop_event.set()
            await self.task  # Finish admitted writes; never cancel and resend them.

    def enqueue(self, project_id, title, body):
        registry = Registry(self.workspace.projects)
        registry.poll()
        if project_id not in registry.projects:
            raise ValueError("PROJECT_NOT_READY")
        if not body.strip():
            raise ValueError("EMPTY_BODY")
        project = registry.projects[project_id]
        if (
            not project.site.recognition or project.site.recognition.publication_kind == "topic"
        ) and not title.strip():
            raise ValueError("EMPTY_TITLE")
        state = State(self.workspace.db)
        try:
            return state.enqueue(
                project,
                {"title": title, "body": body, "target_url": project.site.base_url},
                "desktop:" + uuid.uuid4().hex,
            )
        finally:
            state.close()

    def rows(self):
        state = State(self.workspace.db)
        try:
            return state.rows()
        finally:
            state.close()

    def cancel_pending(self, job):
        state = State(self.workspace.db)
        try:
            with state.db:
                state.db.execute("UPDATE jobs SET status='CANCELLED' WHERE id=? AND status='PENDING'", (job,))
        finally:
            state.close()


async def local_test(workspace, progress):
    """One GUI action owns and stops its loopback server, browser and isolated queue."""
    directory = workspace.create_temp()
    session = knowledge = None
    port = None
    try:
        with serve_readiness() as (server, base):
            port = server.server_port
            workspace.test_ports.add(port)
            knowledge = KnowledgeBase(directory / "knowledge.sqlite")
            session = RecognitionSession(base + "/compose/topic", directory / "profile", knowledge=knowledge)
            progress("Открываю встроенный тестовый сайт…")
            await session.start()
            await session.recognize()
            if session.result.state != "DETECTED":
                raise ValueError("LOCAL_RECOGNITION_FAILED")
            progress("Форма найдена. Проверяю браузерную публикацию…")
            await session.test_publish(
                "Локальная проверка Ж", "Текст & <literal>\n日本語", "local-test", confirmed=True
            )
            if session.result.state != "HTTP_CANDIDATE":
                raise ValueError("LOCAL_BROWSER_FAILED")
            await session.close()
            progress("Браузер закрыт. Проверяю отдельный HTTP…")
            result = await session.verify_http("Локальная HTTP Ж", "HTTP & <literal>\n日本語", confirmed=True)
            if result.status != "SUCCEEDED":
                raise ValueError("LOCAL_HTTP_FAILED")
            projects = directory / "projects"
            session.save(projects / "local-test")
            state = State(directory / "state.sqlite")
            try:
                state.enqueue(
                    load_project(projects / "local-test"),
                    {"title": "Engine Ж", "body": "Engine & <literal>\n日本語"},
                    "local-test",
                )
                progress("Проект сохранён. Запускаю HTTP Engine…")
                await Engine(Registry(projects), state).run(asyncio.Event(), once=True)
                if state.rows()[0]["status"] != "SUCCEEDED" or len(server.posts) != 3:
                    raise ValueError("LOCAL_ENGINE_FAILED")
                return {
                    "status": "SUCCEEDED",
                    "browser": True,
                    "http": True,
                    "engine": True,
                    "publications": len(server.posts),
                    "server_stopped": True,
                }
            finally:
                state.close()
    finally:
        if session:
            await session.close()
        if knowledge:
            knowledge.close()
        workspace.active.discard(directory)
        workspace.test_ports.discard(port)
        workspace.clean_temp()
