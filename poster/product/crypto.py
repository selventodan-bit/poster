"""Bounded authenticated backup envelope; no password in argv, headers or logs."""

import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from poster.dispatcher.models import DispatcherError
from poster.dispatcher.security import plain_path, restrict

MAGIC = b"POSTER-BACKUP-1\x00"
CHUNK = 1024 * 1024
MAX_BYTES = 16 * 1024**3


def validate_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise DispatcherError("BACKUP_PASSPHRASE")


def key(password, salt):
    validate_password(password)
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode("utf-8"))


def encrypt(source, target, password):
    source, target = plain_path(source), plain_path(target)
    salt, nonce = os.urandom(16), os.urandom(12)
    header = MAGIC + salt + nonce
    cipher = Cipher(algorithms.AES(key(password, salt)), modes.GCM(nonce)).encryptor()
    cipher.authenticate_additional_data(header)
    count = 0
    with source.open("rb") as src, target.open("xb") as dst:
        restrict(target)
        dst.write(header)
        while raw := src.read(CHUNK):
            count += len(raw)
            if count > MAX_BYTES:
                raise DispatcherError("BACKUP_LIMIT")
            dst.write(cipher.update(raw))
        dst.write(cipher.finalize())
        dst.write(cipher.tag)
        dst.flush()
        os.fsync(dst.fileno())


def decrypt(source, target, password):
    source, target = plain_path(source), plain_path(target)
    count = source.stat().st_size
    header_size = len(MAGIC) + 28
    if not header_size + 16 <= count <= MAX_BYTES + header_size + 16:
        raise DispatcherError("BACKUP_INVALID")
    created = False
    try:
        with source.open("rb") as src:
            header = src.read(header_size)
            if not header.startswith(MAGIC):
                raise ValueError()
            salt, nonce = header[len(MAGIC) : len(MAGIC) + 16], header[-12:]
            src.seek(-16, 2)
            tag = src.read(16)
            src.seek(header_size)
            cipher = Cipher(algorithms.AES(key(password, salt)), modes.GCM(nonce, tag)).decryptor()
            cipher.authenticate_additional_data(header)
            remaining = count - header_size - 16
            with target.open("xb") as dst:
                created = True
                restrict(target)
                while remaining:
                    raw = src.read(min(CHUNK, remaining))
                    if not raw:
                        raise ValueError()
                    remaining -= len(raw)
                    dst.write(cipher.update(raw))
                dst.write(cipher.finalize())
                dst.flush()
                os.fsync(dst.fileno())
    except Exception as exc:
        if created:
            target.unlink(missing_ok=True)
        if isinstance(exc, DispatcherError) and exc.code == "BACKUP_PASSPHRASE":
            raise
        raise DispatcherError("BACKUP_INVALID") from exc
