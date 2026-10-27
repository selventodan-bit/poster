"""Private key ACLs and link rejection, preserving existing encrypted sessions."""

import ctypes
import os
import subprocess

from cryptography.fernet import Fernet
import pytest

from poster import auth
from poster.dispatcher.models import DispatcherError
from poster.dispatcher.security import windows_sid


def private_acl(path):
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
        return
    from ctypes import wintypes as w

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    advapi.GetNamedSecurityInfoW.argtypes = [
        w.LPWSTR,
        w.DWORD,
        w.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.GetNamedSecurityInfoW.restype = w.DWORD
    advapi.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(w.WORD),
        ctypes.POINTER(w.DWORD),
    ]
    advapi.GetSecurityDescriptorControl.restype = w.BOOL
    advapi.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(w.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(w.BOOL),
    ]
    advapi.GetSecurityDescriptorDacl.restype = w.BOOL
    advapi.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, w.DWORD, w.DWORD]
    advapi.GetAclInformation.restype = w.BOOL
    advapi.GetAce.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = w.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = w.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p

    class AclSize(ctypes.Structure):
        _fields_ = [("count", w.DWORD), ("used", w.DWORD), ("free", w.DWORD)]

    assert (
        advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None, None, None, ctypes.byref(descriptor)) == 0
    )
    try:
        control, revision = w.WORD(), w.DWORD()
        assert advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision))
        assert control.value & 0x1000  # SE_DACL_PROTECTED: inherited grants forbidden.
        present, defaulted, acl = w.BOOL(), w.BOOL(), ctypes.c_void_p()
        assert advapi.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)
        )
        assert present.value and acl.value
        size = AclSize()
        assert advapi.GetAclInformation(acl, ctypes.byref(size), ctypes.sizeof(size), 2)
        assert size.count == 1
        ace = ctypes.c_void_p()
        assert advapi.GetAce(acl, 0, ctypes.byref(ace))
        assert ctypes.c_ubyte.from_address(ace.value).value == 0  # ACCESS_ALLOWED_ACE
        assert ctypes.c_uint32.from_address(ace.value + 4).value == 0x1F01FF  # FILE_ALL_ACCESS
        sid = w.LPWSTR()
        assert advapi.ConvertSidToStringSidW(ctypes.c_void_p(ace.value + 8), ctypes.byref(sid))
        try:
            assert sid.value == windows_sid()  # Expanded SID, independent of SDDL's LA/BA aliases.
        finally:
            kernel.LocalFree(sid)
    finally:
        kernel.LocalFree(descriptor)


def test_new_and_legacy_keys_are_private_and_never_replaced(tmp_path, monkeypatch):
    monkeypatch.delenv("POSTER_SESSION_KEY", raising=False)
    monkeypatch.setattr(auth, "_legacy_keys", [])
    private = tmp_path / "workspace/private"
    home = tmp_path / "oldhome"
    auth.ensure_local_key(private, legacy_home=home)
    original = (private / "session.key").read_bytes()
    cipher = auth.encrypt_session({"value": "synthetic private cookie"})
    private_acl(private)
    private_acl(private / "session.key")
    monkeypatch.delenv("POSTER_SESSION_KEY")
    legacy = home / ".poster/session.key"
    legacy.parent.mkdir(parents=True)
    old = Fernet.generate_key()
    legacy.write_bytes(old)
    old_cipher = Fernet(old).encrypt(b'{"value":"synthetic legacy cookie"}')
    auth.ensure_local_key(private, legacy_home=home)
    assert (private / "session.key").read_bytes() == original and legacy.read_bytes() == old
    assert auth.decrypt_bytes(cipher)["value"] == "synthetic private cookie"
    assert auth.decrypt_bytes(old_cipher)["value"] == "synthetic legacy cookie"
    private_acl(legacy)


def test_key_parent_link_is_refused_without_modifying_target(tmp_path, monkeypatch):
    monkeypatch.delenv("POSTER_SESSION_KEY", raising=False)
    real = tmp_path / "real"
    real.mkdir()
    key = Fernet.generate_key()
    (real / "session.key").write_bytes(key)
    linked = tmp_path / "linked"
    if os.name == "nt":
        assert (
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(linked), str(real)], capture_output=True
            ).returncode
            == 0
        )
    else:
        linked.symlink_to(real, target_is_directory=True)
    try:
        with pytest.raises(DispatcherError):
            auth.ensure_local_key(linked, legacy_home=tmp_path / "oldhome")
        assert (real / "session.key").read_bytes() == key
    finally:
        if os.name == "nt":
            linked.rmdir()
        else:
            linked.unlink()


def test_existing_queue_key_acl_is_repaired_without_regeneration(tmp_path):
    from poster.dispatcher.workspace import queue_key

    root = tmp_path / "dispatcher"
    root.mkdir()
    original = Fernet.generate_key()
    (root / "queue.key").write_bytes(original)
    assert queue_key(root) == original
    assert (root / "queue.key").read_bytes() == original
    private_acl(root)
    private_acl(root / "queue.key")
