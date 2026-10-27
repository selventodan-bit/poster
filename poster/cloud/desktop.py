"""One desktop facade for Maker/Dispatcher; all calls run off the Qt thread."""

import asyncio
from pathlib import Path
import json
import os
import sqlite3
import uuid
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
import httpx

from poster.dispatcher.security import plain_path, private_dir
from poster.storage.drive import Drive
from .auth import DesktopOAuth, SecretStore, default_root
from .crypto import recipient
from .drive_files import DriveFiles, Journal
from .errors import CloudError
from .library import Library
from .package import PortableBundle
from .package import public_resource


class FixtureHTTPTransport(httpx.AsyncBaseTransport):
    """Test-only actual loopback HTTP, never forwards Google/user credentials."""

    def __init__(self, base):
        parsed = urlsplit(base)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or not parsed.port
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            raise CloudError("INTEGRITY")
        self.base = base.rstrip("/")
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        if (
            request.url.host != "www.googleapis.com"
            or request.headers.get("authorization") != "Bearer DRIVE_FIXTURE_ONLY"
        ):
            raise CloudError("INTEGRITY")
        target = self.base + request.url.raw_path.decode("ascii")
        headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
        mapped = httpx.Request(request.method, target, headers=headers, content=await request.aread())
        return await self.inner.handle_async_request(mapped)

    async def aclose(self):
        await self.inner.aclose()


class DesktopCloud:
    def __init__(self, root=None, *, files=None):
        self.fixture = None
        config = os.environ.get("POSTER_CLOUD_TEST_CONFIG")
        if config:
            path = plain_path(config)
            if path.stat().st_size > 10000:
                raise CloudError("INTEGRITY")
            self.fixture = json.loads(path.read_bytes())
            if self.fixture.get("mode") != "drive_fixture" or len(self.fixture.get("nonce", "")) != 32:
                raise CloudError("INTEGRITY")
            root = path.parent / ("isolated-cloud-" + self.fixture["nonce"])
        self.root = plain_path(Path(root) if root else default_root())
        self.store = SecretStore(self.root)
        self.auth = DesktopOAuth(self.store)
        self.client = None
        self.files = files
        self.library = Library(files) if files else None
        self.connection = asyncio.Lock()
        self.cache = self.root / "catalog.sqlite"
        with sqlite3.connect(self.cache) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS bindings(source TEXT PRIMARY KEY,project_id TEXT,version TEXT,folder_id TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS resource_aliases(source TEXT,original TEXT,logical TEXT,PRIMARY KEY(source,original))"
            )

    async def status(self):
        if self.fixture:
            return dict(
                configured=True,
                connected=bool(self.store.get("fixture-connected")),
                protected_by="isolated controlled fixture",
            )
        return self.auth.status()

    async def configure(self, path):
        await self.close()
        self.auth.configure(path)
        return self.auth.status()

    async def connect(self):
        if self.fixture:
            self.store.put("fixture-connected", True)
        else:
            await self.auth.connect()
        await self._library()
        return await self.status()

    async def disconnect(self):
        await self.close()
        if self.fixture:
            self.store.put("fixture-connected", None)
            return dict(local_removed=True, revoked=True)
        return await self.auth.disconnect()

    async def _library(self):
        async with self.connection:
            if self.library:
                return self.library
            if self.fixture:
                if not self.store.get("fixture-connected"):
                    raise CloudError("AUTH_REQUIRED")

                async def token():
                    return "DRIVE_FIXTURE_ONLY"

                transport = FixtureHTTPTransport(self.fixture["url"])
            else:
                token = self.auth.token()
                transport = None
            self.client = httpx.AsyncClient(
                base_url="https://www.googleapis.com/drive/v3/",
                transport=transport,
                timeout=30,
                follow_redirects=False,
            )
            legacy = Drive("", self.root / "cache", token, client=self.client)
            key = self.store.get_or_create("transport-key", lambda: Fernet.generate_key().decode())
            self.files = DriveFiles(legacy, Journal(self.root / "transport", key=key))
            self.library = Library(self.files)
            return self.library

    def _binding(self, source):
        with sqlite3.connect(self.cache) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM bindings WHERE source=?", (str(Path(source).resolve()),)
            ).fetchone()
            return dict(row) if row else None

    def _bind(self, source, value):
        with sqlite3.connect(self.cache) as db:
            db.execute(
                "INSERT OR REPLACE INTO bindings VALUES(?,?,?,?)",
                (str(Path(source).resolve()), value["project_id"], value["version"], value["folder_id"]),
            )

    async def preview_resources(self, source, paths):
        from poster.portability.resources import preview

        return preview(plain_path(source).parent, paths)

    async def include_resources(self, source, paths):
        from poster.portability.blobs import FileBlob, copy_blob

        source = plain_path(source)
        selected = []
        report = await self.preview_resources(source, paths)
        for entry in report["files"]:
            path, name = plain_path(entry["source"]), entry["name"]
            blob = FileBlob.capture(path)
            target = plain_path(source.parent / name)
            if path != target:
                if target.exists() and FileBlob.capture(target).sha256 != blob.sha256:
                    name_path = Path(name)
                    name = public_resource(
                        str(
                            name_path.with_name(name_path.stem + "-" + blob.sha256[:8] + name_path.suffix)
                        ).replace("\\", "/")
                    )
                    target = plain_path(source.parent / name)
                if not target.exists():
                    copy_blob(blob, target)
            selected.append(name)
            with sqlite3.connect(self.cache) as db:
                db.execute(
                    "INSERT OR REPLACE INTO resource_aliases VALUES(?,?,?)", (str(source), str(path), name)
                )
        return selected

    async def publish(self, source, *, resources=None, outputs=(), resolve_conflict=False):
        library = await self._library()
        peer = recipient(self.store).public()
        with sqlite3.connect(self.cache) as db:
            aliases = dict(
                db.execute(
                    "SELECT original,logical FROM resource_aliases WHERE source=?", (str(plain_path(source)),)
                )
            )
        bundle = PortableBundle.capture(
            source, resources=resources, outputs=outputs, recipient=peer, resource_aliases=aliases
        )
        previous = self._binding(source)
        expected = previous["version"] if previous and previous["project_id"] == bundle.project_id else None
        if resolve_conflict:
            folder = await library.find(bundle.project_id)
            if folder:
                history, _ = await library.versions(await library.descriptor(folder))
                expected = history[-1]["version"] if history else None
        value = await library.publish(
            bundle, name=bundle.manifest["name"], expected_version=expected, recipient=peer
        )
        self._bind(source, value)
        return {
            **value,
            "url": library.folder_url(value["folder_id"]),
            "resources": {
                n: bundle.manifest["files"][n] for n in bundle.files if n not in bundle.manifest["graphs"]
            },
            "outputs": bundle.manifest["outputs"],
        }

    async def projects(self):
        library = await self._library()
        return [v async for v in library.projects()]

    async def versions(self, project_id):
        library = await self._library()
        folder = await library.find(project_id)
        if not folder:
            raise CloudError("INTEGRITY")
        values, _ = await library.versions(await library.descriptor(folder))
        return values

    async def open(self, project_id, version=None):
        library = await self._library()
        bundle = await library.fetch(project_id, version)
        folder = await library.find(project_id)
        parent = private_dir(self.root / "opened" / uuid.uuid4().hex)
        path = bundle.materialize(parent / "project")
        value = dict(source=str(path), project_id=project_id, version=bundle.version, folder_id=folder["id"])
        self._bind(path, value)
        return value

    async def diagnostics(self):
        status = await self.status()
        if not status["connected"]:
            return {**status, "drive": "NOT_CONFIGURED"}
        library = await self._library()
        root = await library.root()
        return {**status, "drive": "AVAILABLE", "root_configured": bool(root)}

    async def sync_results(self, dispatcher_root, project_id=None):
        from .admission import claims
        from .results import DesktopArchive, export_managed
        from poster.dispatcher.host import request

        library = await self._library()
        archive = DesktopArchive(dispatcher_root)
        entries = await asyncio.to_thread(request, Path(dispatcher_root), "cloud_exports")
        exported = await export_managed(
            library,
            recipient(self.store),
            [e for e in entries if project_id is None or e["origin"]["project_id"] == project_id],
        )
        imported, pending, rejected = [], 0, 0
        projects = [
            p
            async for p in library.projects()
            if p["status"] == "READY" and (project_id is None or p["project_id"] == project_id)
        ]
        for project in projects:
            descriptor = await library.descriptor(await library.find(project["project_id"]))
            history, _ = await claims(library, descriptor)
            for claim in history:
                for index, plan in enumerate(claim["runs"]):
                    if await library.files.info(plan[2]) is None:
                        pending += 1
                        continue
                    try:
                        imported.append(
                            await archive.import_result(library, claim, index, recipient(self.store))
                        )
                    except CloudError:
                        rejected += 1
        return dict(
            imported=imported,
            pending=pending,
            rejected=rejected,
            mode="DETACHED",
            history=archive.rows(),
            exported=exported,
        )

    async def close(self):
        if self.client:
            await self.client.aclose()
            self.client = None
        self.library = None
        self.files = None
