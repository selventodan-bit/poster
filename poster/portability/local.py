"""User-selected local bindings live outside immutable/public project data."""

from pathlib import Path
import hashlib

from poster.cloud.auth import SecretStore


def profile_binding(source, *, value=...):
    key = "local-profile:" + hashlib.sha256(str(Path(source).resolve()).encode()).hexdigest()
    store = SecretStore()
    if value is not ...:
        store.put(key, value)
    return store.get(key)
