"""Session data is encrypted at rest; the key never belongs in a project folder."""

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet


def session_key() -> bytes:
    key = os.environ.get("POSTER_SESSION_KEY")
    if not key:
        raise ValueError("Set POSTER_SESSION_KEY (poster keygen); keep it outside projects/Drive")
    return key.encode()


def encrypt_session(state: dict) -> bytes:
    return Fernet(session_key()).encrypt(json.dumps(state).encode())


def decrypt_session(path: Path) -> dict:
    return json.loads(Fernet(session_key()).decrypt(path.read_bytes()))


def ensure_local_key(private_dir: Path):
    """Desktop convenience: create a private local key outside all exported projects."""
    if os.environ.get("POSTER_SESSION_KEY"):
        return
    private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = private_dir / "session.key"
    if not path.exists():
        with path.open("xb") as f:
            f.write(Fernet.generate_key())
        path.chmod(0o600)
    os.environ["POSTER_SESSION_KEY"] = path.read_text(encoding="ascii").strip()
