"""Private durable metadata and explicitly owned lifecycle paths."""

import json
import os
import shutil
import uuid

from poster.dispatcher.security import plain_path, private_dir, restrict


def sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def durable(path, value):
    path = plain_path(path)
    private_dir(path.parent)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as f:
            restrict(temporary)
            f.write(json.dumps(value, allow_nan=False).encode())
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def remove_owned(path):
    path = plain_path(path)
    if path.is_dir():
        # Refuse links anywhere before recursive removal, including Windows
        # junctions. Only transaction-owned staging/new roots reach this helper.
        for child in path.rglob("*"):
            plain_path(child)
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def file_identity(path):
    path = plain_path(path)
    if not path.exists():
        return None
    stat = path.stat()
    return [stat.st_dev, stat.st_ino]
