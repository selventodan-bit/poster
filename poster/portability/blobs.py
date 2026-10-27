"""Bounded file-backed data, never executable payloads or an execution queue."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path
import shutil
import uuid

import psutil

from poster.cloud.errors import CloudError
from poster.dispatcher.security import plain_path, private_dir, restrict

CHUNK = 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024 * 1024
MIN_MEMORY = 64 * 1024 * 1024
DISK_RESERVE = 32 * 1024 * 1024


def capacity(root, required, *, memory=None, disk=None):
    if not 0 <= required <= MAX_BUNDLE_BYTES:
        raise CloudError("PACKAGE_LIMIT")
    if (psutil.virtual_memory().available if memory is None else memory) < MIN_MEMORY:
        raise CloudError("MEMORY_LIMIT")
    if (shutil.disk_usage(root).free if disk is None else disk) < required + DISK_RESERVE:
        raise CloudError("DISK_LIMIT")


@dataclass(frozen=True)
class FileBlob:
    path: Path
    size: int
    sha256: str

    def __len__(self):
        return self.size

    @classmethod
    def capture(cls, path, *, limit=MAX_FILE_BYTES):
        path = plain_path(path)
        if not path.is_file() or path.stat().st_size > limit:
            raise CloudError("PACKAGE_LIMIT")
        before = path.stat()
        count, sha = 0, hashlib.sha256()
        with path.open("rb") as stream:
            while raw := stream.read(CHUNK):
                count += len(raw)
                if count > limit:
                    raise CloudError("PACKAGE_LIMIT")
                sha.update(raw)
        after = path.stat()
        if count != before.st_size or (before.st_ino, before.st_mtime_ns, before.st_size) != (
            after.st_ino,
            after.st_mtime_ns,
            after.st_size,
        ):
            raise CloudError("INTEGRITY")
        return cls(path, count, sha.hexdigest())

    def verify(self):
        if self.capture(self.path) != self:
            raise CloudError("INTEGRITY")
        return self


def sha256(value):
    return value.verify().sha256 if isinstance(value, FileBlob) else hashlib.sha256(value).hexdigest()


@contextmanager
def reader(value):
    if isinstance(value, FileBlob):
        value.verify()
        with plain_path(value.path).open("rb") as stream:
            yield stream
    else:
        yield io.BytesIO(value)


def copy_blob(value, destination, *, overwrite=False, readonly=False):
    destination = plain_path(destination)
    private_dir(destination.parent)
    capacity(destination.parent, len(value))
    temporary = destination.with_name(".poster-data-" + uuid.uuid4().hex)
    count, sha = 0, hashlib.sha256()
    try:
        with reader(value) as source, temporary.open("xb") as target:
            restrict(temporary)
            while raw := source.read(CHUNK):
                count += len(raw)
                if count > len(value) or count > MAX_FILE_BYTES:
                    raise CloudError("PACKAGE_LIMIT")
                target.write(raw)
                sha.update(raw)
            target.flush()
            os.fsync(target.fileno())
        if count != len(value) or sha.hexdigest() != sha256(value):
            raise CloudError("INTEGRITY")
        if overwrite:
            os.replace(temporary, destination)
        else:
            os.link(temporary, destination)
        restrict(destination)
    finally:
        temporary.unlink(missing_ok=True)
    if readonly:
        destination.chmod(0o400)
    return FileBlob(destination, count, sha.hexdigest())
