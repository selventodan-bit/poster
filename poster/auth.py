"""Session data is encrypted at rest; the key never belongs in a project folder."""

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_legacy_keys = []


def session_key() -> bytes:
    key = os.environ.get("POSTER_SESSION_KEY")
    if not key:
        raise ValueError("Set POSTER_SESSION_KEY (poster keygen); keep it outside projects/Drive")
    return key.encode()


def encrypt_session(state: dict) -> bytes:
    return Fernet(session_key()).encrypt(json.dumps(state).encode())


def decrypt_session(path: Path) -> dict:
    return decrypt_bytes(path.read_bytes())


def decrypt_bytes(encrypted: bytes) -> dict:
    for key in [session_key(), *_legacy_keys]:
        try:
            return json.loads(Fernet(key).decrypt(encrypted))
        except InvalidToken:
            continue
    raise InvalidToken


def ensure_local_key(private_dir: Path, *, legacy_home: Path | None = None):
    """Desktop convenience: create a private local key outside all exported projects."""
    if os.environ.get("POSTER_SESSION_KEY"):
        Fernet(session_key())
        return
    # On Windows chmod(0600) only changes the readonly attribute, not the DACL.
    # Imports are local: Dispatcher security itself reuses this key service.
    from .dispatcher.security import plain_path, private_dir as secure_dir, restrict

    private_dir = secure_dir(private_dir)
    restrict(private_dir, directory=True)
    path = plain_path(private_dir / "session.key")
    legacy = plain_path((legacy_home or Path.home()) / ".poster" / "session.key")
    # Reuse, never overwrite, the key that can decrypt existing Windows projects.
    if not path.exists() and legacy.is_file():
        value = legacy.read_bytes().strip()
        Fernet(value)
        restrict(legacy)
        os.environ["POSTER_SESSION_KEY"] = value.decode("ascii")
        return
    if not path.exists():
        try:
            with path.open("xb") as f:
                f.write(Fernet.generate_key())
        except FileExistsError:
            pass  # A concurrent first start already created the only key.
    restrict(path)
    value = path.read_bytes().strip()
    Fernet(value)
    if legacy.is_file():
        previous = legacy.read_bytes().strip()
        Fernet(previous)
        restrict(legacy)
        if previous != value and previous not in _legacy_keys:
            _legacy_keys.append(previous)
    os.environ["POSTER_SESSION_KEY"] = value.decode("ascii")
