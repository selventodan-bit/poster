"""Private local state and existing Fernet encryption; no graph-granted rights."""

import json
import os
from pathlib import Path
import stat

from filelock import FileLock

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
    # Win64 HANDLE values must not pass through ctypes' default 32-bit int ABI.
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
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
        windows_dacl(path, directory=directory)
    else:
        path.chmod(0o700 if directory else 0o600)


def windows_dacl(path, *, directory=False):
    """Set the same owner-only protected ACL without spawning console utilities.

    Workers clean their entire process tree before confirming a report; ACL
    enforcement must not recreate console descendants after that cleanup.
    """
    import ctypes
    from ctypes import wintypes as w

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        w.LPCWSTR,
        w.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(w.ULONG),
    ]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
    advapi.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(w.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(w.BOOL),
    ]
    advapi.GetSecurityDescriptorDacl.restype = w.BOOL
    advapi.SetNamedSecurityInfoW.argtypes = [
        w.LPWSTR,
        w.DWORD,
        w.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi.SetNamedSecurityInfoW.restype = w.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor, acl = ctypes.c_void_p(), ctypes.c_void_p()
    present, defaulted = w.BOOL(), w.BOOL()
    sddl = "D:P(A;" + ("OICI" if directory else "") + ";FA;;;" + windows_sid() + ")"

    def failed(number):
        error = DispatcherError("PATH_FORBIDDEN")
        error.system_error = int(number)
        return error

    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ):
        raise failed(ctypes.get_last_error())
    try:
        if (
            not advapi.GetSecurityDescriptorDacl(
                descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)
            )
            or not present
            or not acl.value
        ):
            raise failed(ctypes.get_last_error())
        result = advapi.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, acl, None)
        if result:
            raise failed(result)
    finally:
        kernel.LocalFree(descriptor)


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
        private = private_dir(root / "private")
        with FileLock(str(plain_path(private / "session.key.lock")), timeout=30):
            ensure_local_key(private)
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
