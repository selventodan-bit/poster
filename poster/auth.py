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
