import asyncio
import csv
import io
import json
import logging
import time
from collections import defaultdict

from .content_generator.generator import Generator
from .persistence import State
from .project import Project
from .runtime import Runtime
from .storage.local import Registry

log = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        registry: Registry,
        state: State,
        *,
        content_root=None,
        drive=None,
        max_concurrent_http=10,
        max_per_host=1,
        poll_interval=60,
    ):
        if max_concurrent_http < 1 or max_per_host < 1 or poll_interval <= 0:
            raise ValueError("Concurrency and poll interval must be positive")
        self.registry, self.state = registry, state
        self.generator = Generator(content_root) if content_root else None
        self.drive = drive
        self.poll_interval = poll_interval
        self.global_limit = asyncio.Semaphore(max_concurrent_http)
        self.host_limits = defaultdict(lambda: asyncio.Semaphore(max_per_host))
        self.project_limits = {}
        self.last_start = {}
        self.start_locks = defaultdict(asyncio.Lock)
        self.tasks = set()
        self.scheduled = set()
        self.running = False
        self.stopping = False
        self.poll_count = 0

    def enqueue_targets(self):
        for project in self.registry.projects.values():
            if not project.targets or not project.site.campaign or not self.generator:
                continue
            for row in csv.DictReader(io.StringIO(project.targets)):
                try:
                    seed = int(row.get("seed", "1"))
                    key = f"target:{project.site.id}:{row['id']}:{project.site.campaign}:{seed}"
                    if self.state.has_key(key):
                        continue
                    content = self.generator.generate(project.site.campaign, seed)
                    target_url = row["target_url"]
                    Runtime(project).check_url(target_url)
                    self.state.enqueue(
                        project,
                        {"target_url": target_url},
                        key,
                        content,
                    )
                except (ValueError, KeyError):
                    log.warning("Project %s: target/campaign invalid; not queued", project.site.id)

    async def poll(self):
        if self.drive:
            try:
                await self.drive.sync()
            except Exception:
                log.warning("Drive sync failed; cached projects retained")
        changed = self.registry.poll()
        self.state.register(self.registry.projects)
        self.enqueue_targets()
        self.poll_count += 1
        for p in changed:
            log.info("Project %s loaded revision %.12s", p.site.id, p.revision)
        return changed

    async def execute(self, row):
        project = Project.deserialize(row["snapshot"])
        if project.site.id not in self.project_limits:
            self.project_limits[project.site.id] = asyncio.Semaphore(project.site.schedule.max_concurrent)
        async with self.project_limits[project.site.id], self.global_limit:
            # Removed/disabled projects stop receiving work, including jobs already queued.
            if self.stopping or project.site.id not in self.registry.projects:
                return
            async with self.start_locks[project.site.id]:
                delay = project.site.schedule.min_interval_seconds - (
                    time.monotonic() - self.last_start.get(project.site.id, -1e9)
                )
                if delay > 0:
                    await asyncio.sleep(delay)
                if self.stopping or project.site.id not in self.registry.projects:
                    return
                run_id = self.state.start(row["id"])
                if not run_id:
                    return
                self.last_start[project.site.id] = time.monotonic()
            result = await Runtime(project, host_limits=self.host_limits).run(json.loads(row["variables"]))
            self.state.finish(row["id"], run_id, result)
            log.info("Job %s project %s: %s", row["id"], project.site.id, result.status)

    def schedule(self):
        for row in self.state.pending():
            if row["id"] in self.scheduled or row["project_id"] not in self.registry.projects:
                continue
            self.scheduled.add(row["id"])
            task = asyncio.create_task(self.execute(row))
            self.tasks.add(task)

            def done(t, job_id=row["id"]):
                self.tasks.discard(t)
                self.scheduled.discard(job_id)
                if not t.cancelled() and t.exception():
                    log.error("Job %s interrupted by internal error; recovery required", job_id)

            task.add_done_callback(done)

    async def run(self, stop: asyncio.Event, *, once=False):
        with self.state.engine_lock():
            self.state.recover()
            self.registry.projects = self.state.restore_registry()
            self.running = True
            self.stopping = False
            try:
                while not stop.is_set():
                    await self.poll()
                    self.schedule()
                    if once:
                        if self.tasks:
                            await asyncio.gather(*list(self.tasks))
                        break
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=self.poll_interval)
                    except TimeoutError:
                        pass
            finally:
                # Stop scheduling immediately, finish jobs already running.
                self.stopping = True
                if self.tasks:
                    await asyncio.gather(*list(self.tasks), return_exceptions=True)
                self.running = False

    async def reconcile(self):
        """UNKNOWN is read-only verification, never reset to PENDING automatically."""
        with self.state.engine_lock():
            self.state.recover()
            rows = list(self.state.db.execute("SELECT * FROM jobs WHERE status='UNKNOWN'"))
            for row in rows:
                project = Project.deserialize(row["snapshot"])
                result = await Runtime(project).run(json.loads(row["variables"]), recovery=True)
                with self.state.db:
                    self.state.db.execute(
                        "UPDATE jobs SET status=?,result_url=?,reason=?,updated=? WHERE id=?",
                        (result.status, result.result_url, result.reason, time.time(), row["id"]),
                    )
