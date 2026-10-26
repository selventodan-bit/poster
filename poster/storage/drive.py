"""Official Drive v3 REST adapter, polling only the selected tree, never all of Drive."""

import asyncio
import hashlib
import json
import logging
import re
import shutil
import tempfile
from pathlib import Path

import httpx

from ..models import inside
from ..project import load_project

log = logging.getLogger(__name__)
FOLDER = "application/vnd.google-apps.folder"


class DriveAPIError(ValueError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"Drive API rejected operation: HTTP {status_code}")


class GoogleToken:
    def __init__(self, credentials_file: str | None = None, *, scopes=None, credential=None):
        import google.auth
        from google.oauth2 import credentials, service_account

        scopes = scopes or ["https://www.googleapis.com/auth/drive"]
        self.refresh_lock = asyncio.Lock()
        if credential is not None:
            self.credentials = credential
        elif credentials_file:
            kind = json.loads(Path(credentials_file).read_text())["type"]
            if kind == "service_account":
                self.credentials = service_account.Credentials.from_service_account_file(
                    credentials_file, scopes=scopes
                )
            else:
                self.credentials = credentials.Credentials.from_authorized_user_file(
                    credentials_file, scopes=scopes
                )
        else:
            self.credentials, _ = google.auth.default(scopes=scopes)

    async def __call__(self):
        async with self.refresh_lock:
            if not self.credentials.valid:
                from google.auth.transport.requests import Request

                await asyncio.to_thread(self.credentials.refresh, Request())
            return self.credentials.token


class Drive:
    def __init__(self, root_id: str, cache: Path, token, *, client=None):
        self.root_id, self.cache, self.token = root_id, cache, token
        self.client = client or httpx.AsyncClient(base_url="https://www.googleapis.com/drive/v3/", timeout=30)
        self.own_client = client is None
        self.revisions = {}

    async def close(self):
        if self.own_client:
            await self.client.aclose()

    async def api(self, method, path, *, retry=True, accepted=(), max_bytes=None, **kwargs):
        # New project storage uses this same authenticated transport, but never
        # retries an ambiguous mutation unless its fixed Drive ID proves safety.
        headers = dict(kwargs.pop("headers", {}))
        headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
        attempts = 5 if retry else 1
        for attempt in range(attempts):
            try:
                headers["Authorization"] = f"Bearer {await self.token()}"
                if max_bytes is None:
                    response = await self.client.request(method, path, headers=headers, **kwargs)
                else:
                    async with self.client.stream(method, path, headers=headers, **kwargs) as stream:
                        chunks, size = [], 0
                        async for chunk in stream.aiter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                raise ValueError("Drive response exceeds limit")
                            chunks.append(chunk)
                        response = httpx.Response(
                            stream.status_code,
                            headers=stream.headers,
                            content=b"".join(chunks),
                            request=stream.request,
                        )
            except httpx.TransportError:
                if attempt == attempts - 1:
                    raise ValueError("Drive transport unavailable") from None
                await asyncio.sleep(min(2**attempt, 16))
                continue
            retryable = response.status_code in {429, 500, 502, 503, 504}
            if response.status_code == 403:
                try:
                    reasons = [e.get("reason") for e in response.json()["error"].get("errors", [])]
                    retryable = any(x in {"rateLimitExceeded", "userRateLimitExceeded"} for x in reasons)
                except (ValueError, KeyError):
                    pass
            if retryable and attempt < attempts - 1:
                try:
                    delay = min(max(float(response.headers.get("Retry-After", 0)), 2**attempt), 60)
                except ValueError:
                    delay = min(2**attempt, 16)
                await asyncio.sleep(delay)
                continue
            if response.status_code >= 400 and response.status_code not in accepted:
                raise DriveAPIError(response.status_code)
            return response
        raise ValueError("Drive retries exhausted")

    async def children(self, folder):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", folder):
            raise ValueError("Invalid Drive folder ID")
        files, page = [], None
        while True:
            params = {
                "q": f"'{folder}' in parents and trashed = false",
                "pageSize": 1000,
                "fields": "nextPageToken,files(id,name,mimeType,md5Checksum,modifiedTime)",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page:
                params["pageToken"] = page
            payload = (await self.api("GET", "files", params=params)).json()
            files.extend(payload.get("files", []))
            page = payload.get("nextPageToken")
            if not page:
                names = [f["name"] for f in files]
                if len(names) != len(set(names)):
                    raise ValueError("Ambiguous duplicate names in Drive folder")
                if any(n in {".", ".."} or "/" in n or "\\" in n for n in names):
                    raise ValueError("Unsafe Drive filename")
                return files

    async def tree(self, folder, prefix="", depth=0):
        if depth > 5:
            raise ValueError("Drive tree too deep")
        files = {}
        for f in await self.children(folder):
            if f["name"] in {".", ".."} or "/" in f["name"] or "\\" in f["name"]:
                raise ValueError("Unsafe Drive filename")
            name = prefix + f["name"]
            if f["mimeType"] == FOLDER:
                nested = await self.tree(f["id"], name + "/", depth + 1)
                if files.keys() & nested.keys():
                    raise ValueError("Duplicate Drive filename")
                files.update(nested)
            else:
                if name in files:
                    raise ValueError("Duplicate Drive filename")
                files[name] = f
            if len(files) > 2000:
                raise ValueError("Drive tree too large")
        return files

    async def sync_tree(self, folder, destination, *, project):
        files = await self.tree(folder)
        if "READY" not in files:
            return
        ready = await self.api("GET", f"files/{files['READY']['id']}", params={"alt": "media"})
        ready_hash = hashlib.sha256(ready.content).hexdigest()
        if self.revisions.get(str(destination)) == ready_hash and destination.exists():
            return
        manifest = ready.json()
        self.cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".download-", dir=self.cache) as temp:
            stage = Path(temp) / "tree"
            stage.mkdir()
            checksums = manifest["files"]
            if manifest.get("schema_version") != 1 or len(checksums) > 2000:
                raise ValueError("Invalid Drive manifest")
            for name, expected in checksums.items():
                if name not in files:
                    raise ValueError("Incomplete Drive upload")
                path = inside(stage, name)
                if path.suffix.lower() not in {".yaml", ".yml", ".txt", ".csv", ".enc", ".json"}:
                    raise ValueError("Only declarative data files can be synced")
                data = (await self.api("GET", f"files/{files[name]['id']}", params={"alt": "media"})).content
                if len(data) > 5_000_000 or hashlib.sha256(data).hexdigest() != expected:
                    raise ValueError("Incomplete Drive upload/checksum mismatch")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            (stage / "READY").write_bytes(ready.content)
            if project:
                load_project(stage)
            else:
                from ..content_generator.generator import Generator

                for campaign in (stage / "campaigns").glob("*.yaml"):
                    Generator(stage).generate(campaign.stem, 1)
            destination.parent.mkdir(parents=True, exist_ok=True)
            old = Path(temp) / "old"
            if destination.exists():
                destination.rename(old)
            try:
                stage.rename(destination)
            except Exception:
                if old.exists():
                    old.rename(destination)
                raise
        self.revisions[str(destination)] = ready_hash

    async def sync(self):
        top = {f["name"]: f for f in await self.children(self.root_id) if f["mimeType"] == FOLDER}
        if "projects" in top:
            projects = [f for f in await self.children(top["projects"]["id"]) if f["mimeType"] == FOLDER]
            present = {f["name"] for f in projects}
            for f in projects:
                try:
                    destination = inside(self.cache / "projects", f["name"])
                    await self.sync_tree(f["id"], destination, project=True)
                except (ValueError, KeyError):
                    log.warning("Drive project update rejected; previous revision retained")
            local = self.cache / "projects"
            if local.exists():
                for p in local.iterdir():
                    if p.is_dir() and p.name not in present:
                        shutil.rmtree(p)
        elif (self.cache / "projects").exists():
            shutil.rmtree(self.cache / "projects")
        if "content" in top:
            await self.sync_tree(top["content"]["id"], self.cache / "content", project=False)

    async def upload_backup(self, folder_id: str, snapshot: Path):
        metadata = {"name": snapshot.name, "parents": [folder_id]}
        boundary = "poster-backup-boundary"
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n"
        ).encode()
        body += snapshot.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        # Backup upload is not automatically retried: POST could have succeeded before timeout.
        response = await self.client.post(
            "https://www.googleapis.com/upload/drive/v3/files",
            params={"uploadType": "multipart"},
            headers={
                "Authorization": f"Bearer {await self.token()}",
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            content=body,
        )
        if response.status_code >= 400:
            raise ValueError(f"Drive backup failed: HTTP {response.status_code}")
        return response.json()["id"]

    async def download_backup(self, file_id: str, output: Path):
        if output.exists():
            raise ValueError("Restore destination must not exist")
        data = (await self.api("GET", f"files/{file_id}", params={"alt": "media"})).content
        if not data.startswith(b"SQLite format 3\0"):
            raise ValueError("Not a SQLite snapshot")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
