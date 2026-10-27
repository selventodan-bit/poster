"""Stable per-user state, independent of the installed executable directory."""

import os
from pathlib import Path

from cryptography.fernet import Fernet
from filelock import FileLock

from .models import DispatcherError
from .security import plain_path, private_dir, restrict


def workspace_path():
    if os.environ.get("POSTER_DISPATCHER_WORKSPACE"):
        return plain_path(os.environ["POSTER_DISPATCHER_WORKSPACE"])
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return plain_path(base / "Poster" / "Dispatcher")


def queue_key(root):
    from ..product.locking import connection_lease

    with connection_lease(root):
        return _queue_key(root)


def _queue_key(root):
    """Do not regenerate a missing key for existing encrypted state or use cwd keys."""
    root = private_dir(root)
    restrict(root, directory=True)
    path = plain_path(root / "queue.key")
    with FileLock(str(root / "queue-key.lock"), timeout=10):
        if not path.exists():
            if (root / "catalog.enc").exists() or (root / "dispatcher.sqlite").exists():
                raise DispatcherError("KEY_INVALID")
            with path.open("xb") as stream:
                stream.write(Fernet.generate_key())
            restrict(path)
        try:
            restrict(path)
            from ..auth import read_key

            key = read_key(path)
            Fernet(key)
        except Exception as exc:
            raise DispatcherError("KEY_INVALID") from exc
    os.environ["POSTER_SESSION_KEY"] = key.decode("ascii")
    return key
