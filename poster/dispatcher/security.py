"""Private local state and existing Fernet encryption; no graph-granted rights."""

import json
import os
from pathlib import Path
import stat
import subprocess

from ..auth import decrypt_bytes, encrypt_session, ensure_local_key
from .models import DispatcherError


def plain_path(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise DispatcherError("PATH_FORBIDDEN")
        if part == path and not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise DispatcherError("PATH_FORBIDDEN")
    return path.resolve()


def windows_sid():
    import ctypes
    from ctypes import wintypes

    token = wintypes.HANDLE()
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise DispatcherError("PATH_FORBIDDEN")
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise DispatcherError("PATH_FORBIDDEN")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        value = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(ctypes.c_void_p(sid), ctypes.byref(value)):
            raise DispatcherError("PATH_FORBIDDEN")
        try:
            return value.value
        finally:
            kernel.LocalFree(value)
    finally:
        kernel.CloseHandle(token)


def restrict(path, directory=False):
    path = plain_path(path)
    if os.name == "nt":
        grant = "*" + windows_sid() + (":(OI)(CI)F" if directory else ":F")
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode:
            raise DispatcherError("PATH_FORBIDDEN")
    else:
        path.chmod(0o700 if directory else 0o600)


def private_dir(path):
    path = plain_path(path)
    fresh = not path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if fresh or os.name != "nt":
        restrict(path, directory=True)
    return path


def key_setup(root, key_file=None):
    try:
        if key_file:
            from cryptography.fernet import Fernet

            raw = plain_path(key_file).read_bytes().strip()
            Fernet(raw)
            os.environ["POSTER_SESSION_KEY"] = raw.decode("ascii")
        ensure_local_key(private_dir(root / "private"))
    except Exception as exc:
        raise DispatcherError("KEY_INVALID") from exc


def seal(value):
    return encrypt_session(value)


def unseal(raw):
    try:
        return decrypt_bytes(raw)
    except Exception as exc:
        raise DispatcherError("KEY_INVALID") from exc


def safe_json(path, value):
    from ..maker.storage import atomic

    plain_path(path)
    atomic(path, json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode())
    restrict(path)
