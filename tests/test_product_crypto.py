"""Real authenticated encryption and bounded backup I/O; synthetic private bytes."""

import hashlib
import os

import pytest

from poster.dispatcher.models import DispatcherError
from poster.product import crypto

PASSWORD = "synthetic backup passphrase 2026"


def test_streamed_backup_encryption_authenticates_before_use(tmp_path):
    source, archive, restored = (tmp_path / n for n in ["source", "backup.pbackup", "restored"])
    block = b"PRIVATE_SYNTHETIC_BACKUP_COOKIE" * 1024
    with source.open("wb") as f:
        for _ in range(700):
            f.write(block)
    crypto.encrypt(source, archive, PASSWORD)
    assert block[:29] not in archive.read_bytes()
    crypto.decrypt(archive, restored, PASSWORD)
    with source.open("rb") as a, restored.open("rb") as b:
        assert hashlib.file_digest(a, "sha256").digest() == hashlib.file_digest(b, "sha256").digest()
    if os.name != "nt":
        assert restored.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("mutation", ["password", "header", "ciphertext", "tag", "truncate"])
def test_wrong_key_or_damaged_envelope_never_leaves_plaintext(tmp_path, mutation):
    source, archive, restored = (tmp_path / n for n in ["source", "backup", "restored"])
    source.write_bytes(b"PRIVATE_SYNTHETIC_BACKUP_KEY" * 100)
    crypto.encrypt(source, archive, PASSWORD)
    raw = bytearray(archive.read_bytes())
    if mutation == "truncate":
        raw = raw[:-1]
    elif mutation != "password":
        index = {"header": len(crypto.MAGIC) + 3, "ciphertext": len(crypto.MAGIC) + 35, "tag": -1}[mutation]
        raw[index] ^= 1
    archive.write_bytes(raw)
    with pytest.raises(DispatcherError) as caught:
        crypto.decrypt(archive, restored, PASSWORD + "wrong" if mutation == "password" else PASSWORD)
    assert caught.value.code == "BACKUP_INVALID"
    assert not restored.exists()


def test_invalid_backup_never_deletes_existing_destination(tmp_path):
    source, archive, target = (tmp_path / n for n in ["source", "backup", "existing"])
    source.write_bytes(b"synthetic")
    crypto.encrypt(source, archive, PASSWORD)
    target.write_bytes(b"USER FILE")
    with pytest.raises(DispatcherError):
        crypto.decrypt(archive, target, PASSWORD)
    assert target.read_bytes() == b"USER FILE"
    archive.write_bytes(b"invalid")
    with pytest.raises(DispatcherError):
        crypto.decrypt(archive, target, PASSWORD)
    assert target.read_bytes() == b"USER FILE"
    with pytest.raises(FileExistsError):
        crypto.encrypt(source, target, PASSWORD)
    assert target.read_bytes() == b"USER FILE"


def test_password_requirement_and_bound_are_enforced(tmp_path, monkeypatch):
    source, archive = tmp_path / "source", tmp_path / "backup"
    source.write_bytes(b"a" * 100)
    with pytest.raises(DispatcherError) as caught:
        crypto.encrypt(source, archive, "short")
    assert caught.value.code == "BACKUP_PASSPHRASE" and not archive.exists()
    monkeypatch.setattr(crypto, "MAX_BYTES", 50)
    with pytest.raises(DispatcherError) as caught:
        crypto.encrypt(source, archive, PASSWORD)
    assert caught.value.code == "BACKUP_LIMIT"
