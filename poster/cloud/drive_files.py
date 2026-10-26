"""Bounded Drive files over the existing REST adapter, with resumable SHA-verified data.

Drive server-generated IDs provide atomic create-if-absent. A run claim is never
retried after an ambiguous POST. Upload retries concern data bytes, never graphs.
"""

import asyncio
from pathlib import Path
from urllib.parse import urlsplit

from filelock import FileLock

from poster.dispatcher.security import private_dir, restrict, seal, unseal
from poster.maker.storage import atomic
from poster.storage.drive import Drive, DriveAPIError, FOLDER

from .errors import CloudError
from .format import MAX_BYTES, MAX_METADATA, canonical, digest, identifier, metadata

FIELDS = "id,name,mimeType,parents,properties,size,sha256Checksum,trashed,version,capabilities(canEdit)"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
BINARY = "application/octet-stream"


class Journal:
    """Private encrypted transport recovery; never a Drive queue or public state."""

    def __init__(self, root):
        self.path = private_dir(Path(root)) / "drive-uploads.enc"

    def get(self, key):
        with FileLock(str(self.path) + ".lock"):
            data = unseal(self.path.read_bytes()) if self.path.exists() else {}
            return data.get(key)

    def set(self, key, value):
        with FileLock(str(self.path) + ".lock"):
            data = unseal(self.path.read_bytes()) if self.path.exists() else {}
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
            raw = seal(data)
            if len(raw) > 5_000_000:
                raise CloudError("PACKAGE_LIMIT")
            atomic(self.path, raw)
            restrict(self.path)


class DriveFiles:
    def __init__(self, transport: Drive, journal: Journal):
        self.transport, self.journal = transport, journal
        self.gate = asyncio.Semaphore(3)

    async def ids(self, count=1):
        if not 1 <= count <= 1000:
            raise CloudError("PACKAGE_LIMIT")
        response = await self.transport.api(
            "GET",
            "files/generateIds",
            params={"count": count, "space": "drive", "type": "files"},
            max_bytes=MAX_METADATA,
        )
        values = response.json().get("ids", [])
        if len(values) != count or len(set(values)) != count:
            raise CloudError("INTEGRITY")
        return [identifier(v) for v in values]

    async def info(self, file_id):
        response = await self.transport.api(
            "GET",
            "files/" + identifier(file_id),
            accepted=(404,),
            params={"fields": FIELDS, "supportsAllDrives": "true"},
            max_bytes=MAX_METADATA,
        )
        if response.status_code == 404:
            return None
        value = response.json()
        if value.get("id") != file_id or value.get("trashed") is True:
            raise CloudError("INTEGRITY")
        return value

    async def list(self, parent=None, *, properties=None, folders=None):
        clauses = ["trashed = false"]
        if parent:
            clauses.append("'" + identifier(parent) + "' in parents")
        for key, value in (properties or {}).items():
            identifier(key)
            identifier(value)
            clauses.append("properties has { key='" + key + "' and value='" + value + "' }")
        if folders is not None:
            clauses.append("mimeType " + ("=" if folders else "!=") + " '" + FOLDER + "'")
        seen, page = set(), None
        while True:
            params = dict(
                q=" and ".join(clauses),
                fields="nextPageToken,files(" + FIELDS + ")",
                pageSize=1000,
                supportsAllDrives="true",
                includeItemsFromAllDrives="true",
            )
            if page:
                params["pageToken"] = page
            value = (await self.transport.api("GET", "files", params=params, max_bytes=2_000_000)).json()
            for item in value.get("files", []):
                file_id = identifier(item["id"])
                if file_id not in seen:
                    seen.add(file_id)
                    yield item
            next_page = value.get("nextPageToken")
            if not next_page:
                break
            if next_page == page:
                raise CloudError("INTEGRITY")
            page = next_page

    async def read(self, file_id, *, limit=MAX_BYTES):
        info = await self.info(file_id)
        if (
            not info
            or info["mimeType"] in {FOLDER, "application/vnd.google-apps.shortcut"}
            or int(info.get("size", 0)) > limit
        ):
            raise CloudError("INTEGRITY")
        response = await self.transport.api(
            "GET",
            "files/" + identifier(file_id),
            params={"alt": "media", "supportsAllDrives": "true"},
            max_bytes=limit,
        )
        raw = response.content
        if info.get("sha256Checksum") and digest(raw) != info["sha256Checksum"]:
            raise CloudError("INTEGRITY")
        return raw

    async def json(self, file_id):
        return metadata(await self.read(file_id, limit=MAX_METADATA))

    async def folder(self, name, parent=None, *, file_id=None, properties=None):
        file_id = file_id or (await self.ids())[0]
        value = dict(id=file_id, name=name[:200], mimeType=FOLDER, properties=properties or {})
        if parent:
            value["parents"] = [identifier(parent)]
        try:
            response = await self.transport.api(
                "POST",
                "files",
                retry=False,
                accepted=(409,),
                params={"fields": FIELDS, "supportsAllDrives": "true"},
                json=value,
                max_bytes=MAX_METADATA,
            )
        except (ValueError, DriveAPIError):
            existing = await self.info(file_id)
            if existing is None:
                raise CloudError("DRIVE_UNAVAILABLE") from None
        else:
            existing = await self.info(file_id) if response.status_code == 409 else response.json()
        if (
            not existing
            or existing.get("mimeType") != FOLDER
            or (parent and parent not in existing.get("parents", []))
            or any(existing.get("properties", {}).get(k) != v for k, v in (properties or {}).items())
        ):
            raise CloudError("DRIVE_CONFLICT")
        return existing

    async def create_json(self, file_id, name, parent, value, *, strict_new=False, properties=None):
        """Unique server ID is the linearization point, not a lock-file name."""
        raw = canonical(value)
        if len(raw) > MAX_METADATA:
            raise CloudError("PACKAGE_LIMIT")
        boundary = "poster-cloud-json-boundary"
        meta = canonical(
            dict(
                id=identifier(file_id),
                name=name,
                parents=[identifier(parent)],
                mimeType="application/json",
                properties=properties or {},
            )
        )
        body = (
            b"--"
            + boundary.encode()
            + b"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            + meta
            + b"\r\n--"
            + boundary.encode()
            + b"\r\nContent-Type: application/json\r\n\r\n"
            + raw
            + b"\r\n--"
            + boundary.encode()
            + b"--\r\n"
        )
        try:
            response = await self.transport.api(
                "POST",
                UPLOAD,
                retry=False,
                accepted=(409,),
                params={"uploadType": "multipart", "supportsAllDrives": "true", "fields": FIELDS},
                headers={"Content-Type": "multipart/related; boundary=" + boundary},
                content=body,
                max_bytes=MAX_METADATA,
            )
        except ValueError:
            # A claim's uncertain admission can never lead to executing a graph.
            if strict_new:
                raise CloudError("RUN_UNCERTAIN") from None
            if await self.info(file_id) is None:
                raise CloudError("DRIVE_UNAVAILABLE") from None
            response = None
        if response is not None and response.status_code != 409:
            if response.json().get("id") != file_id:
                raise CloudError("INTEGRITY")
            if digest(await self.read(file_id, limit=MAX_METADATA)) != digest(raw):
                raise CloudError("INTEGRITY")
            return True
        if strict_new:
            raise CloudError("RUN_ALREADY_CLAIMED")
        if digest(await self.read(file_id, limit=MAX_METADATA)) != digest(raw):
            raise CloudError("DRIVE_CONFLICT")
        return False

    async def upload(self, file_id, name, parent, raw, *, mime=BINARY, properties=None):
        identifier(file_id)
        identifier(parent)
        if len(raw) > MAX_BYTES:
            raise CloudError("PACKAGE_LIMIT")
        async with self.gate:
            expected = digest(raw)
            existing = await self.info(file_id)
            if existing:
                if parent not in existing.get("parents", []) or digest(await self.read(file_id)) != expected:
                    raise CloudError("DRIVE_CONFLICT")
                self.journal.set(file_id, None)
                return existing
            record = self.journal.get(file_id)
            if record and record["sha256"] != expected:
                raise CloudError("DRIVE_CONFLICT")
            location = record.get("location") if record else None
            if not location:
                value = dict(
                    id=file_id, name=name, parents=[parent], mimeType=mime, properties=properties or {}
                )
                try:
                    response = await self.transport.api(
                        "POST",
                        UPLOAD,
                        retry=False,
                        params={"uploadType": "resumable", "supportsAllDrives": "true", "fields": FIELDS},
                        json=value,
                        headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(len(raw))},
                        max_bytes=MAX_METADATA,
                    )
                except ValueError:
                    raise CloudError("DRIVE_UNAVAILABLE") from None
                location = response.headers.get("Location", "")
                parsed = urlsplit(location)
                if (
                    parsed.scheme != "https"
                    or parsed.hostname != "www.googleapis.com"
                    or not parsed.path.startswith("/upload/drive/v3/")
                    or parsed.username
                    or parsed.fragment
                ):
                    raise CloudError("INTEGRITY")
                record = dict(sha256=expected, location=location)
                self.journal.set(file_id, record)
                offset = 0
            else:
                offset = await self._offset(location, len(raw), file_id, expected)
            failures = 0
            while offset < len(raw) or not raw:
                end = min(offset + 1024 * 1024, len(raw))
                content_range = f"bytes {offset}-{end - 1}/{len(raw)}" if raw else "bytes */0"
                try:
                    response = await self.transport.api(
                        "PUT",
                        location,
                        retry=False,
                        accepted=(308, 404, 410),
                        headers={"Content-Type": mime, "Content-Range": content_range},
                        content=raw[offset:end],
                        max_bytes=MAX_METADATA,
                    )
                    if response.status_code in {404, 410}:
                        self.journal.set(file_id, None)
                        raise CloudError("DRIVE_UNAVAILABLE")
                    if response.status_code in {200, 201}:
                        break
                    new_offset = self._range(response, len(raw))
                    if new_offset <= offset:
                        raise ValueError()
                    offset = new_offset
                except CloudError:
                    raise
                except ValueError:
                    failures += 1
                    if failures > 4:
                        raise CloudError("DRIVE_UNAVAILABLE") from None
                    await asyncio.sleep(min(2 ** (failures - 1), 8))
                    offset = await self._offset(location, len(raw), file_id, expected)
                    if offset == len(raw):
                        break
            actual = await self.read(file_id)
            if digest(actual) != expected:
                raise CloudError("INTEGRITY")
            self.journal.set(file_id, None)
            return await self.info(file_id)

    @staticmethod
    def _range(response, total):
        value = response.headers.get("Range", "")
        if not value:
            return 0
        try:
            prefix, end = value.split("-")
            offset = int(end) + 1
            if prefix != "bytes=0" or not 0 <= offset <= total:
                raise ValueError()
            return offset
        except ValueError as exc:
            raise CloudError("INTEGRITY") from exc

    async def _offset(self, location, total, file_id, expected):
        response = await self.transport.api(
            "PUT",
            location,
            retry=False,
            accepted=(308, 404, 410),
            content=b"",
            headers={"Content-Range": f"bytes */{total}"},
            max_bytes=MAX_METADATA,
        )
        if response.status_code in {200, 201}:
            if digest(await self.read(file_id)) != expected:
                raise CloudError("INTEGRITY")
            return total
        if response.status_code != 308:
            self.journal.set(file_id, None)
            raise CloudError("DRIVE_UNAVAILABLE")
        return self._range(response, total)
