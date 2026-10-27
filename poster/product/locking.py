"""OS-backed application leases and offline maintenance, outside restored trees."""

from contextlib import contextmanager, ExitStack
import hashlib
import os
from pathlib import Path
import tempfile

from filelock import FileLock, Timeout

from poster.dispatcher.security import plain_path, private_dir
from poster.dispatcher.models import DispatcherError

ROLES = ("maker", "gui", "service")


def lock_path(root):
    root = plain_path(root)
    key = hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()
    return root.parent / ".poster-product-locks" / key


def lock_root(root):
    return private_dir(lock_path(root))


def start_lock():
    # Only startup coordination is temporary. Restore markers/journals are
    # persistent beside the app data, so a reboot cannot silently clear them.
    if os.name == "nt":
        from poster.dispatcher.security import windows_sid

        user = windows_sid()
    else:
        user = str(os.getuid())
    suffix = hashlib.sha256(user.encode()).hexdigest()[:20]
    folder = private_dir(Path(tempfile.gettempdir()) / ("poster-product-start-" + suffix))
    return FileLock(str(folder / "startup.lock"), timeout=5)


def ancestors(root):
    root = plain_path(root)
    home = Path.home().resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    return [
        p
        for p in [root, *root.parents]
        if p != Path(p.anchor) and not home.is_relative_to(p) and not temporary.is_relative_to(p)
    ]


@contextmanager
def application_lease(root, role):
    if role not in ROLES:
        raise ValueError("Unknown product role")
    lease = None
    try:
        with start_lock(), ExitStack() as guards:
            folders = [lock_path(p) for p in ancestors(root)]
            if any((f / "restore-pending.json").exists() for f in folders):
                raise DispatcherError("RESTORE_RECOVERY")
            own = lock_root(root)
            for folder in sorted(set([own, *[f for f in folders if f.is_dir()]]), key=str):
                guards.enter_context(FileLock(str(folder / "maintenance.lock"), timeout=0))
                if (folder / "restore-pending.json").exists():
                    raise DispatcherError("RESTORE_RECOVERY")
            lease = FileLock(str(own / (role + ".lock")), timeout=0)
            lease.acquire()
        try:
            yield
        finally:
            lease.release()
    except Timeout as exc:
        raise DispatcherError("PRODUCT_BUSY") from exc


def child_lock_folders(root):
    if not root.is_dir():
        return
    pending = [root]
    count = 0
    while pending:
        parent = pending.pop()
        with os.scandir(parent) as entries:
            for entry in entries:
                path = Path(entry.path)
                count += 1
                if count > 40000:
                    raise DispatcherError("BACKUP_LIMIT")
                if parent == root and entry.name in {"browsers", "browser-cache", "tmp", "__pycache__"}:
                    continue
                if entry.name == ".poster-product-locks":
                    for folder in plain_path(path).iterdir():
                        if folder.is_dir():
                            yield plain_path(folder)
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(plain_path(path))


@contextmanager
def offline(roots, *, allow_recovery=False):
    """Lease all nested apps; preserve the original service/CLI lock checks."""
    roots = sorted(set(map(plain_path, roots)), key=str)
    try:
        with ExitStack() as stack:
            with start_lock():
                folders = {lock_root(r) for r in roots}
                for folder in sorted(folders, key=str):
                    stack.enter_context(FileLock(str(folder / "maintenance.lock"), timeout=0))
                children = {f for root in roots for f in child_lock_folders(root)} - folders
                for folder in sorted(children, key=str):
                    stack.enter_context(FileLock(str(folder / "maintenance.lock"), timeout=0))
                for folder in sorted(folders | children, key=str):
                    if not allow_recovery and (folder / "restore-pending.json").exists():
                        raise DispatcherError("RESTORE_RECOVERY")
                    for role in ROLES:
                        stack.enter_context(FileLock(str(folder / (role + ".lock")), timeout=0))
                for root in roots:
                    if root.is_dir():
                        for existing in [
                            root / "service.lock",
                            root / "dispatcher.sqlite.dispatcher.lock",
                            root / "state.sqlite.engine.lock",
                        ]:
                            if existing.exists():
                                with FileLock(str(plain_path(existing)), timeout=0):
                                    pass
            yield
    except Timeout as exc:
        raise DispatcherError("PRODUCT_BUSY") from exc
