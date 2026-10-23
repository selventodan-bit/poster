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
    private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = private_dir / "session.key"
    legacy = (legacy_home or Path.home()) / ".poster" / "session.key"
    # Reuse, never overwrite, the key that can decrypt existing Windows projects.
    if not path.exists() and legacy.is_file():
        value = legacy.read_bytes().strip()
        Fernet(value)
        os.environ["POSTER_SESSION_KEY"] = value.decode("ascii")
        return
    if not path.exists():
        with path.open("xb") as f:
            f.write(Fernet.generate_key())
        path.chmod(0o600)
    value = path.read_bytes().strip()
    Fernet(value)
    if legacy.is_file():
        previous = legacy.read_bytes().strip()
        Fernet(previous)
        if previous != value and previous not in _legacy_keys:
            _legacy_keys.append(previous)
    os.environ["POSTER_SESSION_KEY"] = value.decode("ascii")
