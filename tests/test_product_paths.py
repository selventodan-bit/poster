"""Updates retain state; OS locks prevent maintenance of active applications."""

import json
import subprocess
import sys

from filelock import FileLock
import pytest

from poster.dispatcher.models import DispatcherError
from poster.product import paths
from poster.product.locking import application_lease, lock_root, offline


def test_new_frozen_install_uses_stable_data_and_adopts_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "product_home", lambda: tmp_path / "user-state")
    monkeypatch.delenv("POSTER_MAKER_WORKSPACE", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    first, second = tmp_path / "first-install", tmp_path / "updated-install"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    assert paths.maker_root() == tmp_path / "user-state/Maker"
    legacy = first / ".poster"
    (legacy / "maker-projects").mkdir(parents=True)
    (legacy / "private").mkdir()
    (legacy / "private/session.key").write_bytes(b"unchanged-key")
    assert paths.maker_root() == legacy
    monkeypatch.chdir(second)
    assert paths.maker_root() == legacy
    assert (legacy / "private/session.key").read_bytes() == b"unchanged-key"
    (tmp_path / "user-state/maker-location.json").write_text("{}")
    with pytest.raises(DispatcherError):
        paths.maker_root()


def test_real_application_lock_blocks_backup_and_recovers_on_exit(tmp_path):
    root = tmp_path / "workspace"
    program = 'from poster.product.locking import application_lease;import sys;from pathlib import Path\nwith application_lease(Path(sys.argv[1]),"maker"):\n print("READY",flush=True);sys.stdin.readline()'
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert process.stdout.readline().strip() == "READY"
        with pytest.raises(DispatcherError) as caught:
            with offline([root]):
                pass
        assert caught.value.code == "PRODUCT_BUSY"
    finally:
        process.communicate("\n", timeout=10)
    assert process.returncode == 0
    with offline([root]):
        with pytest.raises(DispatcherError):
            with application_lease(root, "maker"):
                pass


def test_old_service_lock_and_interrupted_restore_are_refused(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    with FileLock(str(root / "service.lock")):
        with pytest.raises(DispatcherError):
            with offline([root]):
                pass
    (lock_root(root) / "restore-pending.json").write_text(json.dumps(dict(pending=True)))
    with pytest.raises(DispatcherError) as caught:
        with application_lease(root, "gui"):
            pass
    assert caught.value.code == "RESTORE_RECOVERY"
    (lock_root(root) / "restore-pending.json").unlink()


def test_source_and_explicit_workspace_remain_supported(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "product_home", lambda: tmp_path / "user")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("POSTER_MAKER_WORKSPACE", raising=False)
    assert paths.maker_root() == tmp_path / ".poster"
    monkeypatch.setenv("POSTER_MAKER_WORKSPACE", str(tmp_path / "explicit"))
    assert paths.maker_root() == tmp_path / "explicit"


def test_parent_maintenance_covers_active_and_new_nested_applications(tmp_path):
    parent = tmp_path / "parent"
    child = parent / "nested"
    child.mkdir(parents=True)
    with application_lease(child, "service"):
        with pytest.raises(DispatcherError):
            with offline([parent]):
                pass
    with offline([parent]):
        with pytest.raises(DispatcherError):
            with application_lease(child, "service"):
                pass
    # A parent's durable recovery marker also blocks an absent nested root;
    # startup must not recreate a partially restored tree.
    (lock_root(parent) / "restore-pending.json").write_text("{}")
    missing = parent / "missing"
    try:
        with pytest.raises(DispatcherError) as caught:
            with application_lease(missing, "service"):
                pass
        assert caught.value.code == "RESTORE_RECOVERY" and not missing.exists()
    finally:
        (lock_root(parent) / "restore-pending.json").unlink()


def test_runner_cannot_forge_product_maintenance_namespace(tmp_path):
    from poster.maker.resources import FileSandbox
    from poster.maker.model import GraphError

    sandbox = FileSandbox(tmp_path / "run")
    with pytest.raises(GraphError):
        sandbox.write(".poster-product-locks/forged/restore-pending.json", b"{}")
    assert not (tmp_path / "run/.poster-product-locks").exists()
