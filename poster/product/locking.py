"""OS-backed application leases and offline maintenance, outside restored trees."""

from contextlib import contextmanager, ExitStack
import hashlib
import os

from filelock import FileLock, Timeout

from poster.dispatcher.security import plain_path, private_dir
from poster.dispatcher.models import DispatcherError

ROLES = ("maker", "gui", "service")


def lock_root(root):
    root = plain_path(root)
    identity = os.path.normcase(str(root))
    key = hashlib.sha256(identity.encode()).hexdigest()
    return private_dir(root.parent / ".poster-product-locks" / key)


@contextmanager
def application_lease(root, role):
    if role not in ROLES:
        raise ValueError("Unknown product role")
    folder = lock_root(root)
    try:
        with FileLock(str(folder / "maintenance.lock"), timeout=0):
            if (folder / "restore-pending.json").exists():
                raise DispatcherError("RESTORE_RECOVERY")
            lease = FileLock(str(folder / (role + ".lock")), timeout=0)
            lease.acquire()
        try:
            yield
        finally:
            lease.release()
    except Timeout as exc:
        raise DispatcherError("PRODUCT_BUSY") from exc


@contextmanager
def offline(roots):
    """Refuse running apps and the original pre-6 service/CLI ownership locks."""
    try:
        with ExitStack() as stack:
            for root in sorted(set(map(plain_path, roots)), key=str):
                folder = lock_root(root)
                stack.enter_context(FileLock(str(folder / "maintenance.lock"), timeout=0))
                for role in ROLES:
                    stack.enter_context(FileLock(str(folder / (role + ".lock")), timeout=0))
                if root.is_dir():
                    # Check old application ownership without retaining files
                    # inside a directory that Windows must atomically rename.
                    for existing in [root / "service.lock", root / "dispatcher.sqlite.dispatcher.lock"]:
                        if existing.exists():
                            with FileLock(str(plain_path(existing)), timeout=0):
                                pass
            yield
    except Timeout as exc:
        raise DispatcherError("PRODUCT_BUSY") from exc
