"""Private scratch ownership survives real process death; never delete by glob alone."""

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import uuid

import psutil

from poster.dispatcher.models import DispatcherError
from poster.dispatcher.security import plain_path, restrict, private_dir
from .atomic import durable, file_identity, remove_owned, sync_directory
from .locking import registry_path


def record_path(identity):
    if not isinstance(identity, str) or not re.fullmatch("[a-f0-9]{32}", identity):
        raise DispatcherError("RESTORE_RECOVERY")
    return registry_path() / ("scratch-" + identity + ".json")


def base():
    root = private_dir(registry_path())
    restrict(root, directory=True)
    result = private_dir(root / "scratch")
    restrict(result, directory=True)
    return result


def checked(identity):
    try:
        record = record_path(identity)
        if record.stat().st_size > 20000:
            raise ValueError()
        data = json.loads(plain_path(record).read_bytes())
        expected = registry_path() / "scratch" / (".poster-" + data["kind"] + "-" + identity)
        if data["path"] != str(expected):
            raise ValueError()
        path = plain_path(expected)
        if (
            data["format"] != "poster.scratch.v1"
            or data["id"] != identity
            or data["kind"] not in {"backup", "verify"}
            or data.get("phase") not in {"ACTIVE", "CLEANING"}
            or type(data.get("pid")) is not int
            or data["pid"] < 1
            or type(data.get("birth")) not in {int, float}
            or not math.isfinite(data["birth"])
            or data["birth"] <= 0
            or path.name != ".poster-" + data["kind"] + "-" + identity
        ):
            raise ValueError()
        if not path.exists() and data["phase"] == "CLEANING":
            return data
        if file_identity(path) != data["identity"]:
            raise ValueError()
        marker_path = plain_path(path / "owner.json")
        if marker_path.exists():
            marker = json.loads(marker_path.read_bytes())
            if marker != {k: v for k, v in data.items() if k != "phase"}:
                raise ValueError()
        elif data["phase"] != "CLEANING":
            raise ValueError()
        return data
    except Exception as exc:
        raise DispatcherError("RESTORE_RECOVERY") from exc


def owner_alive(data):
    try:
        process = psutil.Process(data["pid"])
        return process.create_time() == data["birth"] and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True  # Permission failure never authorizes deletion.


def cleanup(identity, *, own=False):
    record = record_path(identity)
    if not record.exists():
        return False
    data = checked(identity)
    if not own and owner_alive(data):
        return False
    if own and (data["pid"] != os.getpid() or data["birth"] != psutil.Process().create_time()):
        raise DispatcherError("RESTORE_RECOVERY")
    data["phase"] = "CLEANING"
    durable(record, data)
    if Path(data["path"]).exists():
        remove_owned(Path(data["path"]))
    record.unlink()
    sync_directory(record.parent)
    return True


def cleanup_abandoned():
    registry = registry_path()
    if not registry.is_dir():
        return 0
    count = 0
    for i, record in enumerate(registry.glob("scratch-*.json")):
        if i >= 30000:
            raise DispatcherError("BACKUP_LIMIT")
        identity = record.name[len("scratch-") : -len(".json")]
        if cleanup(identity):
            count += 1
    return count


@contextmanager
def directory(kind):
    if kind not in {"backup", "verify"}:
        raise ValueError("Unknown scratch kind")
    parent = base()
    identity = uuid.uuid4().hex
    record = record_path(identity)
    if record.exists():
        raise DispatcherError("RESTORE_RECOVERY")
    path = parent / (".poster-" + kind + "-" + identity)
    path.mkdir(mode=0o700)
    restrict(path, directory=True)
    data = dict(
        format="poster.scratch.v1",
        id=identity,
        kind=kind,
        path=str(path),
        identity=file_identity(path),
        pid=os.getpid(),
        birth=psutil.Process().create_time(),
    )
    created = False
    try:
        durable(path / "owner.json", data)
        private_dir(record.parent)
        with record.open("xb") as f:
            created = True
            restrict(record)
            f.write(json.dumps(dict(**data, phase="ACTIVE"), allow_nan=False).encode())
            f.flush()
            os.fsync(f.fileno())
        sync_directory(record.parent)
    except BaseException:
        remove_owned(path)
        if created:
            record.unlink(missing_ok=True)
        raise
    try:
        yield path, identity
    finally:
        cleanup(identity, own=True)
