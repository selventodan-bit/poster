import hashlib
import json

import httpx
import pytest

from poster.storage.drive import Drive, FOLDER
from poster.storage.local import Registry
from test_project_queue import publish


class FakeDriveAPI:
    """Protocol-level fake of Drive v3; real authorization still needs an operator account."""

    def __init__(self):
        self.children = {"root": [{"id": "projects", "name": "projects", "mimeType": FOLDER}], "projects": []}
        self.media = {}
        self.calls = []
        self.fail_once = None
        self.deleted = False

    def project(self, folder):
        self.children["projects"] = [{"id": "classic", "name": "classic", "mimeType": FOLDER}]
        self.children["classic"] = []
        for path in folder.iterdir():
            data = path.read_bytes()
            self.media[path.name] = data
            self.children["classic"].append(
                {
                    "id": path.name,
                    "name": path.name,
                    "mimeType": "application/octet-stream",
                    "md5Checksum": hashlib.md5(data).hexdigest(),
                }
            )

    def __call__(self, req):
        assert req.headers["authorization"] == "Bearer test-token"
        self.calls.append(req)
        if self.fail_once:
            status = self.fail_once
            self.fail_once = None
            return httpx.Response(status, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}})
        if req.url.path.endswith("/files"):
            query = req.url.params["q"]
            folder = query.split("'")[1]
            items = self.children[folder]
            # Deliberate pagination even on small folders.
            if not req.url.params.get("pageToken") and len(items) > 1:
                return httpx.Response(200, json={"files": items[:1], "nextPageToken": "page-2"})
            return httpx.Response(
                200, json={"files": items[1:] if req.url.params.get("pageToken") else items}
            )
        return httpx.Response(200, content=self.media[req.url.path.rsplit("/", 1)[-1]])


async def token():
    return "test-token"


@pytest.mark.parametrize("retry_code", [429, 503, 403])
async def test_drive_pagination_backoff_hotload_and_cache(tmp_path, make_project, monkeypatch, retry_code):
    source = tmp_path / "source"
    publish(make_project("https://safe.test"), source)
    api = FakeDriveAPI()
    api.project(source)
    api.fail_once = retry_code
    delays = []

    async def no_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("poster.storage.drive.asyncio.sleep", no_sleep)
    async with httpx.AsyncClient(
        base_url="https://www.googleapis.com/drive/v3/", transport=httpx.MockTransport(api)
    ) as client:
        drive = Drive("root", tmp_path / "cache", token, client=client)
        registry = Registry(tmp_path / "cache/projects")
        registry.poll()
        assert not registry.projects
        await drive.sync()
        assert delays
        assert len(registry.poll()) == 1
        assert registry.projects["classic"].site.id == "classic"
        calls = len([r for r in api.calls if r.url.path.endswith("/site.yaml")])
        await drive.sync()
        assert len([r for r in api.calls if r.url.path.endswith("/site.yaml")]) == calls
        assert any(r.url.params.get("pageToken") == "page-2" for r in api.calls)
        api.children["projects"] = []
        await drive.sync()
        registry.poll()
        assert not registry.projects


async def test_drive_incomplete_upload_preserves_previous_revision(tmp_path, make_project):
    source = tmp_path / "source"
    publish(make_project("https://safe.test"), source)
    api = FakeDriveAPI()
    api.project(source)
    async with httpx.AsyncClient(
        base_url="https://www.googleapis.com/drive/v3/", transport=httpx.MockTransport(api)
    ) as client:
        drive = Drive("root", tmp_path / "cache", token, client=client)
        await drive.sync()
        reg = Registry(tmp_path / "cache/projects")
        reg.poll()
        old = reg.projects["classic"].revision
        # New READY is uploaded, one corresponding file still has old bytes.
        ready = json.loads(api.media["READY"])
        ready["files"]["site.yaml"] = "0" * 64
        api.media["READY"] = json.dumps(ready).encode()
        await drive.sync()
        reg.poll()
        assert reg.projects["classic"].revision == old
        assert not list((tmp_path / "cache").glob(".download-*"))


async def test_drive_data_only_paths_and_auth_denial(tmp_path):
    api = FakeDriveAPI()
    api.children["projects"] = [{"id": "classic", "name": "classic", "mimeType": FOLDER}]
    api.children["classic"] = [
        {"id": "READY", "name": "READY", "mimeType": "text/plain"},
        {"id": "evil.py", "name": "evil.py", "mimeType": "text/plain"},
    ]
    code = b"raise SystemExit('remote code must never run')"
    api.media = {
        "READY": json.dumps(
            {"schema_version": 1, "files": {"evil.py": hashlib.sha256(code).hexdigest()}}
        ).encode(),
        "evil.py": code,
    }
    async with httpx.AsyncClient(
        base_url="https://www.googleapis.com/drive/v3/", transport=httpx.MockTransport(api)
    ) as client:
        drive = Drive("root", tmp_path / "cache", token, client=client)
        await drive.sync()
        assert not (tmp_path / "cache/projects/classic").exists()
    async with httpx.AsyncClient(
        base_url="https://www.googleapis.com/drive/v3/",
        transport=httpx.MockTransport(lambda _: httpx.Response(401)),
    ) as client:
        with pytest.raises(ValueError, match="HTTP 401"):
            await Drive("root", tmp_path / "cache", token, client=client).sync()
