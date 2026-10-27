"""OS leases with verified PID/birth, independent of renamed application data."""

from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from filelock import FileLock, Timeout
import psutil

from poster.dispatcher.security import plain_path, private_dir, restrict, safe_json
from poster.dispatcher.models import DispatcherError

ROLES = ("maker", "gui", "service")


def registry_path():
    if os.environ.get("POSTER_PRODUCT_LOCK_HOME"):
        return plain_path(os.environ["POSTER_PRODUCT_LOCK_HOME"])
    base = (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        if os.name == "nt"
        else Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    )
    return plain_path(base / ".poster-product-locks")


def lock_path(root):
    root = plain_path(root)
    return registry_path() / hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()


def legacy_lock_path(root):
    root = plain_path(root)
    return (
        root.parent
        / ".poster-product-locks"
        / hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()
    )


def legacy_folders(roots):
    home = Path.home().resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    return {
        legacy_lock_path(p)
        for root in roots
        for p in ancestors(root)
        if p != Path(p.anchor)
        and not home.is_relative_to(p)
        and not temp.is_relative_to(p)
        and legacy_lock_path(p).is_dir()
    }


def lock_root(root):
    root = plain_path(root)
    registry = private_dir(registry_path())
    restrict(registry, directory=True)
    folder = private_dir(lock_path(root))
    index = folder / "root.json"
    if not index.exists():
        safe_json(index, dict(format="poster.lease.root.v1", path=str(root)))
    return folder


def verified_owner(path):
    try:
        data = json.loads(plain_path(path).read_bytes())
        if (
            data["format"] != "poster.lease.owner.v1"
            or type(data["pid"]) is not int
            or type(data["birth"]) not in {int, float}
            or data.get("role") not in {*ROLES, "database", "maintenance", "registration", "startup"}
        ):
            return None
        process = psutil.Process(data["pid"])
        if process.create_time() != data["birth"] or process.status() == psutil.STATUS_ZOMBIE:
            return None
        return dict(pid=data["pid"], birth=data["birth"], role=data["role"], verified=True)
    except (OSError, ValueError, KeyError, TypeError, OverflowError, psutil.Error):
        return None


class Lease:
    def __init__(self, folder, name, role, *, timeout=0, unique=False):
        folder = plain_path(folder)
        self.path = plain_path(folder / name)
        if self.path.exists() and self.path.stat().st_nlink != 1:
            raise DispatcherError("PATH_FORBIDDEN")
        self.owner = self.path.with_name(name + ".owner.json")
        self.unique = unique
        self.owned = False
        self.lock = FileLock(str(self.path), timeout=timeout)
        try:
            self.lock.acquire()
            self.owned = True
        except Timeout as exc:
            error = DispatcherError("PRODUCT_BUSY")
            error.owner = verified_owner(self.owner)
            raise error from exc
        try:
            safe_json(
                self.owner,
                dict(
                    format="poster.lease.owner.v1",
                    pid=os.getpid(),
                    birth=psutil.Process().create_time(),
                    role=role,
                ),
            )
        except BaseException:
            self.close()
            raise

    def close(self):
        lock = getattr(self, "lock", None)
        if lock is None:
            return
        if not self.owned:
            self.lock = None
            return
        lock.release()
        self.owned = False
        self.lock = None
        if self.unique:
            for path in (self.path, self.owner):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def start_lock():
    user = (
        str(os.getuid())
        if os.name != "nt"
        else __import__("poster.dispatcher.security", fromlist=["windows_sid"]).windows_sid()
    )
    folder = private_dir(
        Path(tempfile.gettempdir())
        / ("poster-product-start-" + hashlib.sha256(user.encode()).hexdigest()[:20])
    )
    return Lease(folder, "startup.lock", "startup", timeout=5)


def ancestors(root):
    root = plain_path(root)
    return [root, *root.parents]


def register(root, name, role, *, unique=False):
    with start_lock(), ExitStack() as guards:
        folders = [lock_path(p) for p in ancestors(root)] + list(legacy_folders([root]))
        if any((f / "restore-pending.json").exists() for f in folders):
            raise DispatcherError("RESTORE_RECOVERY")
        own = lock_root(root)
        for folder in sorted(set([own, *[f for f in folders if f.is_dir()]]), key=str):
            guards.enter_context(Lease(folder, "maintenance.lock", "registration"))
            if (folder / "restore-pending.json").exists():
                raise DispatcherError("RESTORE_RECOVERY")
        return Lease(own, name, role, unique=unique)


def connection_lease(root):
    return register(root, "connection-" + uuid.uuid4().hex + ".lock", "database", unique=True)


@contextmanager
def application_lease(root, role):
    if role not in ROLES:
        raise ValueError("Unknown product role")
    with register(root, role + ".lock", role):
        yield


def child_lock_folders(roots):
    registry = private_dir(registry_path())
    count = 0
    for folder in registry.iterdir():
        count += 1
        if count > 30000:
            raise DispatcherError("BACKUP_LIMIT")
        if not re.fullmatch("[a-f0-9]{64}", folder.name):
            continue
        folder = plain_path(folder)
        index = plain_path(folder / "root.json")
        try:
            if not index.is_file() or index.stat().st_size > 20000:
                continue
            data = json.loads(index.read_bytes())
            path = Path(data["path"])
            if (
                data["format"] != "poster.lease.root.v1"
                or not path.is_absolute()
                or hashlib.sha256(os.path.normcase(str(path)).encode()).hexdigest() != folder.name
            ):
                raise ValueError()
            if any(path.is_relative_to(root) for root in roots):
                yield folder
        except FileNotFoundError:
            continue
        except (KeyError, ValueError, TypeError) as exc:
            raise DispatcherError("RESTORE_RECOVERY") from exc


@contextmanager
def offline(roots, *, allow_recovery=False):
    roots = sorted(set(map(plain_path, roots)), key=str)
    with ExitStack() as stack:
        with start_lock():
            folders = {lock_root(root) for root in roots} | set(child_lock_folders(roots))
            for folder in sorted(legacy_folders(roots), key=str):
                folder = plain_path(folder)
                if folder in folders:
                    continue
                if not allow_recovery and (folder / "restore-pending.json").exists():
                    raise DispatcherError("RESTORE_RECOVERY")
                outside = not any(folder.is_relative_to(root) for root in roots)
                if outside:
                    stack.enter_context(Lease(folder, "maintenance.lock", "maintenance"))
                else:
                    try:
                        with FileLock(str(folder / "maintenance.lock"), timeout=0):
                            pass
                    except Timeout as exc:
                        raise DispatcherError("PRODUCT_BUSY") from exc
                for role in ROLES:
                    try:
                        with FileLock(str(folder / (role + ".lock")), timeout=0):
                            pass
                    except Timeout as exc:
                        raise DispatcherError("PRODUCT_BUSY") from exc
            for folder in sorted(folders, key=str):
                stack.enter_context(Lease(folder, "maintenance.lock", "maintenance"))
                if not allow_recovery and (folder / "restore-pending.json").exists():
                    raise DispatcherError("RESTORE_RECOVERY")
                for role in ROLES:
                    stack.enter_context(Lease(folder, role + ".lock", "maintenance"))
                for path in folder.glob("connection-*.lock"):
                    stack.enter_context(Lease(folder, path.name, "maintenance"))
            for root in roots:
                if not root.is_dir():
                    continue
                for existing in [
                    root / "service.lock",
                    root / "dispatcher.sqlite.dispatcher.lock",
                    root / "state.sqlite.engine.lock",
                ]:
                    if existing.exists():
                        try:
                            with FileLock(str(plain_path(existing)), timeout=0):
                                pass
                        except Timeout as exc:
                            raise DispatcherError("PRODUCT_BUSY") from exc
        yield
